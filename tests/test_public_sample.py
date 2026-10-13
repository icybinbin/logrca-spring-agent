from __future__ import annotations

import hashlib
import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from logrca.file_app import analyze_upload

ROOT = Path(__file__).resolve().parents[1]


def test_public_excerpt_retains_source_and_has_inspectable_citations(tmp_path):
    path = ROOT / "data" / "public_hikari_1546_excerpt.log"
    content = path.read_text(encoding="utf-8")
    manifest = json.loads((ROOT / "data" / "public_hikari_1546_source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(content.encode()).hexdigest() == manifest["sha256"]
    assert len(content.split()) <= 25
    assert "github.com/brettwooldridge/HikariCP/issues/1546" in manifest["source_url"]
    assert manifest["confirmed_root_cause"] is None
    result = analyze_upload(content.encode(), path.name, output_root=tmp_path)
    assert not result.run.errors
    assert result.run.event_count == 1
    report = result.run.reports[0]
    assert report.is_anomaly is True
    row = result.source_events[report.incident_id][0]
    assert row["source_line"] == 1 and row["end_line"] == 3
    assert row["timezone_assumed"] is True
    assert "org.springframework" in row["message"]
    assert all(ref.line_id == row["line_id"] for ref in report.evidence)


def test_public_sample_is_available_in_web_without_invented_origin():
    app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=20).run()
    app.selectbox(key="sample").select("公开故障日志节选 · HikariCP").run()
    assert any("公开Issue" in item.value for item in app.caption)
    app.button(key="run_analysis").click().run()
    assert not app.exception and not app.error
    result = app.session_state["file_analysis"]
    assert result.filename == "public_hikari_1546_excerpt.log"
    assert result.run.successful_incident_count == 1
