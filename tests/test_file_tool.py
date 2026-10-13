from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from logrca.cli import main
from logrca.file_app import MAX_UPLOAD_BYTES, analyze_upload
from logrca.normalizer import load_loghub_csv
from logrca.pipeline import analyze_path
from logrca.spring_json_parser import parse_spring_json
from logrca.spring_parser import parse_spring_log

ROOT = Path(__file__).resolve().parents[1]


def test_boot_file_preserves_timezone_stack_and_physical_lines(tmp_path):
    source = tmp_path / "boot.log"
    source.write_text(
        "2026-10-13T10:00:00.001+08:00 ERROR 1234 --- [orders] [main] c.example.OrderService : HikariPool-1 - Connection is not available, request timed out after 30000ms\n"
        "\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:196)\n"
        "2026-10-13T10:00:00.002+08:00 ERROR 1234 --- [orders] [main] c.example.OrderService : SQLTransientConnectionException unable to acquire database connection\n",
        encoding="utf-8")
    incident = parse_spring_log(source)
    assert len(incident.events) == 2
    event = incident.events[0]
    assert event.service == "orders"
    assert event.timestamp.utcoffset().total_seconds() == 8 * 3600
    assert event.metadata["start_line"] == 1
    assert event.metadata["end_line"] == 2
    assert event.metadata["timezone_assumed"] is False
    assert "HikariPool.java:196" in event.message
    run = analyze_path(source)
    assert run.reports[0].root_cause_code == "db_pool_exhausted"


def test_boot_without_app_name_and_ansi_is_readable(tmp_path):
    source = tmp_path / "catalog.log"
    source.write_text("\x1b[32m2026-10-13 10:00:00.001 INFO 1234 --- [main] c.example.CatalogService : request completed\x1b[0m\n", encoding="utf-8")
    event = parse_spring_log(source).events[0]
    assert event.service == "catalog"
    assert event.metadata["timezone_assumed"] is True
    assert "\x1b" not in event.message


def test_boot_correlation_is_preserved(tmp_path):
    source = tmp_path / "traced.log"
    source.write_text("2026-10-13T10:00:00.001Z INFO 1234 --- [orders] [main] [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb] c.example.OrderService : request accepted\n", encoding="utf-8")
    assert parse_spring_log(source).events[0].trace_id == "a" * 32


@pytest.mark.parametrize("text", [
    "this file is not a log\n",
    "2026-10-13T10:00:00.001Z ERROR missing pid and logger\n",
    "2026-10-13T10:00:00.001Z INFO 1234 --- [main] c.example.App : good header\n2026-10-13T10:00:00.002Z ERROR invalid custom header\n",
])
def test_unsupported_layout_is_an_error_not_a_normal_report(tmp_path, text):
    source = tmp_path / "unsupported.log"
    source.write_text(text, encoding="utf-8")
    run = analyze_path(source)
    assert run.reports == []
    assert run.errors[0].stage == "parse"
    assert run.normal_count == 0
    assert main(["analyze", "--path", str(source), "--output-dir", str(tmp_path / "out")]) == 1


def test_json_auto_detection_and_source_inspection(tmp_path):
    content = (ROOT / "data" / "spring_db_pool.jsonl").read_bytes()
    result = analyze_upload(content, "spring.log", output_root=tmp_path)
    assert result.run.detected_format == "spring_json"
    report = result.run.reports[0]
    assert report.root_cause_code == "db_pool_exhausted"
    rows = result.source_events[report.incident_id]
    source_by_id = {row["line_id"]: row for row in rows}
    for reference in report.evidence:
        assert reference.line_id in source_by_id
        assert source_by_id[reference.line_id]["source_line"] in {2, 3}
    assert "HikariPool.java" in source_by_id["SPRING-00002"]["message"]
    assert result.traces[report.incident_id]
    assert result.run.model_telemetry == {}


def test_logstash_flat_fields_blank_lines_and_bom(tmp_path):
    source = tmp_path / "events.jsonl"
    row = {"@timestamp": "2026-10-13T10:00:00.111037681+08:00", "level": "INFO",
        "message": "request accepted", "service.name": "catalog", "trace.id": "opaque-123"}
    source.write_text("\n" + json.dumps(row) + "\n", encoding="utf-8-sig")
    event = parse_spring_json(source).events[0]
    assert event.line_id == "SPRING-00002"
    assert event.service == "catalog"
    assert event.trace_id == "opaque-123"
    assert event.timestamp.microsecond == 111037


@pytest.mark.parametrize("row", [
    {"message": "missing timestamp", "level": "ERROR"},
    {"@timestamp": "2026-10-13T10:00:00.123", "message": "no timezone", "level": "ERROR"},
    {"@timestamp": "invalid", "message": "bad time", "level": "ERROR"},
    {"@timestamp": "2026-10-13T10:00:00Z", "message": "unknown level", "level": "BOGUS"},
    ["not an object"],
])
def test_malformed_json_fields_fail_before_diagnosis(tmp_path, row):
    source = tmp_path / "bad.jsonl"
    source.write_text(json.dumps(row), encoding="utf-8")
    run = analyze_path(source)
    assert run.reports == []
    assert run.errors[0].stage == "parse"


@pytest.mark.parametrize("content,filename,match", [
    (b"", "empty.log", "为空"), (b" \n", "empty.log", "为空"),
    (b"x" * (MAX_UPLOAD_BYTES + 1), "big.log", "5MB"),
    (b"\xff\xfeabc", "encoded.log", "UTF-8"),
    (b"x", "script.exe", "格式"),
], ids=["empty", "whitespace", "oversize", "encoding", "extension"])
def test_upload_bounds_are_enforced(tmp_path, content, filename, match):
    with pytest.raises(ValueError, match=match):
        analyze_upload(content, filename, output_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_archive_citations_are_inspectable_and_upload_is_not_retained(tmp_path):
    content = (ROOT / "data" / "spring_db_pool.log").read_bytes().replace(b"HikariPool-1", b"HikariPool-1 password=private-pass")
    result = analyze_upload(content, "../../outside/production.log", output_root=tmp_path)
    assert result.filename == "production.log"
    assert not list(tmp_path.rglob("production.log"))
    with zipfile.ZipFile(io.BytesIO(result.download_archive())) as archive:
        names = archive.namelist()
        assert "run-summary.json" in names
        assert "source-evidence.json" in names
        assert not any(".." in name or name.endswith(".log") for name in names)
        for name in names:
            assert b"private-pass" not in archive.read(name)
        summary = json.loads(archive.read("run-summary.json"))
        assert summary["source_path"] == "production.log"
        report = summary["reports"][0]
        assert report["trace_path"] in names
        rows = json.loads(archive.read("source-evidence.json"))[report["incident_id"]]
        assert {ref["line_id"] for ref in report["evidence"]} <= {row["line_id"] for row in rows}


def test_failed_model_run_still_exports_trace_without_fake_report(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    result = analyze_upload((ROOT / "data" / "spring_db_pool.log").read_bytes(), "spring.log", output_root=tmp_path, backend="deepseek")
    assert result.run.reports == []
    assert result.run.failed_incident_count == 1
    assert next(iter(result.run.model_telemetry.values()))["status"] == "failed"
    with zipfile.ZipFile(io.BytesIO(result.download_archive())) as archive:
        assert any(name.startswith("traces/") for name in archive.namelist())
        assert not any(name.startswith("reports/") for name in archive.namelist())


def test_csv_multiline_content_keeps_physical_line_numbers(tmp_path):
    source = tmp_path / "data.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["LineId", "Content", "Level"])
        writer.writeheader()
        writer.writerow({"LineId": "1", "Content": "first line\nstack frame", "Level": "ERROR"})
        writer.writerow({"LineId": "2", "Content": "next event", "Level": "INFO"})
    events = load_loghub_csv(source).events
    assert events[0].metadata["start_line"] == 2
    assert events[0].metadata["end_line"] == 3
    assert events[1].metadata["start_line"] == 4


def test_web_page_requires_click_and_displays_source_and_downloads():
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    assert not app.exception
    assert "file_analysis" not in app.session_state
    app.button(key="run_analysis").click().run()
    assert not app.exception
    assert not app.error
    result = app.session_state["file_analysis"]
    assert result.run.reports[0].root_cause_code == "db_pool_exhausted"
    assert len(app.get("download_button")) == 3
    selected = app.selectbox(key=f"evidence-{result.run.reports[0].incident_id}").value
    assert selected in {ref.line_id for ref in result.run.reports[0].evidence}
    assert any("HikariPool.java" in code.value for code in app.code)


def test_web_json_sample_and_switching_settings_does_not_call_model(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    app.selectbox(key="sample").select("连接池超时 · Spring JSON").run()
    app.button(key="run_analysis").click().run()
    assert app.session_state["file_analysis"].run.detected_format == "spring_json"
    app.radio(key="backend").set_value("DeepSeek").run()
    assert not app.exception
    assert app.session_state["file_analysis"].run.backend == "offline"
    app.button(key="run_analysis").click().run()
    assert not app.exception
    assert app.error
    assert app.session_state["file_analysis"].run.reports == []
