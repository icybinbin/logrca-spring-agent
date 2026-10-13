from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

import logrca.pipeline as pipeline_module
from logrca.agents import IncidentManager
from logrca.models import Incident
from logrca.pipeline import analyze_path


def _write_spring_fixture(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "2026-09-21 10:00:00.001 ERROR [order-service] [trace=known-001] ROOT_A - Hikari database pool exhausted: no idle connection; acquisition timeout",
                "\tat com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:196)",
                "2026-09-21 10:00:00.002 ERROR [order-service] [trace=known-001] SUPPORT_A - SQLTransientConnectionException unable to acquire database connection",
                "2026-09-21 10:00:00.003 CRITICAL [novel-service] [trace=unknown-001] NOVEL_A - Byzantine widget entered an impossible state",
                "2026-09-21 10:00:00.004 INFO [catalog-service] [trace=normal-001] REQUEST_OK - Catalog request completed normally",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_csv_fixture(path: Path) -> None:
    fields = [
        "LineId",
        "Date",
        "Time",
        "Level",
        "Component",
        "EventId",
        "EventTemplate",
        "Content",
        "Label",
    ]
    rows = [
        {
            "LineId": "1",
            "Date": "2026-09-21",
            "Time": "10:00:00.001",
            "Level": "ERROR",
            "Component": "order-service",
            "EventId": "E100",
            "EventTemplate": "database pool saturated",
            "Content": "trace-aaaaaaaaaaaaaaaa Hikari database pool saturated with no idle connection",
            "Label": "-",
        },
        {
            "LineId": "2",
            "Date": "2026-09-21",
            "Time": "10:00:00.002",
            "Level": "ERROR",
            "Component": "order-service",
            "EventId": "E101",
            "EventTemplate": "SQL connection failed",
            "Content": "trace-aaaaaaaaaaaaaaaa SQLTransientConnectionException while obtaining connection",
            "Label": "-",
        },
        {
            "LineId": "3",
            "Date": "2026-09-21",
            "Time": "10:00:00.003",
            "Level": "CRITICAL",
            "Component": "novel-service",
            "EventId": "E200",
            "EventTemplate": "novel subsystem event",
            "Content": "trace-bbbbbbbbbbbbbbbb Byzantine widget entered an impossible state",
            "Label": "X",
        },
        {
            "LineId": "4",
            "Date": "2026-09-21",
            "Time": "10:00:00.004",
            "Level": "INFO",
            "Component": "catalog-service",
            "EventId": "E300",
            "EventTemplate": "request completed",
            "Content": "trace-cccccccccccccccc catalog request completed normally",
            "Label": "-",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _assert_complete_three_incident_run(result: pipeline_module.AnalysisRun) -> None:
    assert result.event_count == 4
    assert result.discovered_incident_count == 3
    assert result.incident_count == 3
    assert result.successful_incident_count == 3
    assert result.failed_incident_count == 0
    assert result.anomaly_count == 2
    assert result.normal_count == 1
    assert result.known_root_count == 1
    assert result.unknown_root_count == 1
    assert len(result.reports) == 3
    assert not result.errors
    roots = {report.root_cause_code for report in result.reports if report.is_anomaly}
    assert roots == {"db_pool_exhausted", None}
    normal = next(report for report in result.reports if not report.is_anomaly)
    assert normal.tool_calls == 0


def test_spring_file_runs_parse_correlate_diagnose_and_persist(
    tmp_path: Path,
) -> None:
    source = tmp_path / "application.log"
    output = tmp_path / "analysis-output"
    _write_spring_fixture(source)

    result = analyze_path(source, format="auto", mode="multi", output_dir=output)

    assert result.detected_format == "spring"
    assert result.input_line_count == 5  # four events plus one stack continuation
    _assert_complete_three_incident_run(result)
    assert result.run_summary_path is not None
    assert Path(result.run_summary_path).is_file()
    assert len(result.report_paths) == 3
    for saved in result.report_paths.values():
        report_path = Path(saved)
        assert report_path.is_file()
        assert report_path.parent == (output / "reports").resolve()
        assert "/" not in report_path.name and "\\" not in report_path.name
    summary = json.loads(Path(result.run_summary_path).read_text(encoding="utf-8"))
    assert summary["known_root_count"] == 1
    assert summary["unknown_root_count"] == 1
    assert len(summary["reports"]) == 3


def test_structured_csv_auto_detection_runs_the_same_complete_flow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "service_structured.csv"
    _write_csv_fixture(source)

    result = analyze_path(source, format="auto", mode="single")

    assert result.detected_format == "loghub_csv"
    assert result.input_line_count == 4  # header is not a log record
    _assert_complete_three_incident_run(result)
    assert result.output_dir is None
    assert result.run_summary_path is None
    assert result.report_paths == {}


def test_limits_bound_input_and_agent_work(tmp_path: Path) -> None:
    source = tmp_path / "bounded.csv"
    _write_csv_fixture(source)

    row_limited = analyze_path(source, limit=2, max_incidents=None)
    incident_limited = analyze_path(source, max_incidents=2)

    assert row_limited.input_line_count == 2
    assert row_limited.event_count == 2
    assert row_limited.discovered_incident_count == 1
    assert row_limited.incident_count == 1
    assert row_limited.known_root_count == 1
    assert incident_limited.discovered_incident_count == 3
    assert incident_limited.incident_count == 2
    assert incident_limited.skipped_incident_count == 1
    assert len(incident_limited.reports) == 2


def test_one_diagnosis_failure_is_recorded_and_later_incidents_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "continue.log"
    _write_spring_fixture(source)
    original = IncidentManager.diagnose
    calls = 0

    def flaky_diagnose(
        self: IncidentManager,
        incident: Incident,
        mode: str = "multi",
        trace_dir: str | Path | None = None,
    ):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated single-incident failure")
        return original(self, incident, mode=mode, trace_dir=trace_dir)

    monkeypatch.setattr(IncidentManager, "diagnose", flaky_diagnose)
    result = analyze_path(source)

    assert calls == 3
    assert result.incident_count == 3
    assert result.failed_incident_count == 1
    assert result.successful_incident_count == 2
    assert len(result.reports) == 2
    assert len(result.errors) == 1
    assert result.errors[0].stage == "diagnose"
    assert result.errors[0].incident_id is not None
    assert "simulated" in result.errors[0].message


def test_agent_controlled_incident_id_cannot_escape_report_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "unsafe.log"
    output = tmp_path / "safe-output"
    _write_spring_fixture(source)

    def malicious_correlation(events):  # type: ignore[no-untyped-def]
        return [
            Incident(
                incident_id="../../outside/report",
                title="Untrusted incident ID",
                events=list(events),
                source="test",
            )
        ]

    monkeypatch.setattr(pipeline_module, "build_incidents", malicious_correlation)
    result = analyze_path(source, output_dir=output)

    assert result.successful_incident_count == 1
    saved = Path(next(iter(result.report_paths.values())))
    assert saved.parent == (output / "reports").resolve()
    assert ".." not in saved.name
    assert not (tmp_path / "outside").exists()


def test_empty_input_returns_a_structured_parse_error(tmp_path: Path) -> None:
    source = tmp_path / "empty.log"
    source.write_text("", encoding="utf-8")

    result = analyze_path(source)

    assert result.event_count == 0
    assert result.incident_count == 0
    assert not result.reports
    assert result.errors[0].stage == "parse"
    assert result.errors[0].exception_type == "EmptyInput"


def test_invocation_errors_are_rejected_before_work(tmp_path: Path) -> None:
    source = tmp_path / "valid.log"
    _write_spring_fixture(source)

    with pytest.raises(FileNotFoundError):
        analyze_path(tmp_path / "missing.log")
    with pytest.raises(ValueError, match="mode"):
        analyze_path(source, mode="invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="limit"):
        analyze_path(source, limit=0)
    with pytest.raises(ValueError, match="max_incidents"):
        analyze_path(source, max_incidents=0)
    with pytest.raises(ValueError, match="unsupported format"):
        analyze_path(source, format="binary")
