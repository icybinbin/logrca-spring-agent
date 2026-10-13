from __future__ import annotations

from pathlib import Path

from logrca.agents import IncidentManager
from logrca.spring_parser import mine_template, parse_spring_log


DEMO_FILE = Path(__file__).resolve().parents[1] / "data" / "spring_db_pool.log"


def test_spring_parser_merges_java_stack_trace() -> None:
    incident = parse_spring_log(DEMO_FILE)
    assert len(incident.events) == 6
    pool_event = next(event for event in incident.events if event.template_id == "DB_POOL_EXHAUSTED")
    assert "SQLTransientConnectionException" in pool_event.message
    assert "HikariPool.java:696" in pool_event.message
    assert pool_event.metadata["start_line"] == 3


def test_template_miner_replaces_dynamic_values() -> None:
    first = mine_template("Order 7319 timed out after 30000ms from 10.0.0.8")
    second = mine_template("Order 8888 timed out after 12000ms from 10.0.0.9")
    assert first == second
    assert "<*>" in first


def test_raw_spring_file_reaches_same_diagnosis() -> None:
    incident = parse_spring_log(DEMO_FILE)
    report = IncidentManager().diagnose(incident, mode="multi")
    assert report.root_cause_code == "db_pool_exhausted"
    assert [item.kind for item in report.evidence] == ["direct", "support"]
