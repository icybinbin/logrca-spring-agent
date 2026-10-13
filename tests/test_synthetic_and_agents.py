from __future__ import annotations

import json
from pathlib import Path

import pytest

from logrca.agents import IncidentManager
from logrca.detector import AnomalyDetector
from logrca.evaluation import compare_modes
from logrca.synthetic import SCENARIOS, build_scenario


def test_scenarios_are_deterministic_and_ground_truth_is_separate() -> None:
    first = build_scenario("db_pool_exhausted")
    second = build_scenario("db_pool_exhausted")
    assert first == second
    assert len(SCENARIOS) == 6
    dumped_events = json.dumps(
        [event.model_dump(mode="json") for event in first.incident.events]
    )
    assert "root_cause_code" not in dumped_events
    assert "evidence_ids" not in dumped_events


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_agent_visible_case_identity_does_not_reveal_scenario(scenario: str) -> None:
    case = build_scenario(scenario)
    visible_identity = json.dumps(
        {
            "incident_id": case.incident.incident_id,
            "title": case.incident.title,
            "line_ids": [event.line_id for event in case.incident.events],
            "trace_ids": [event.trace_id for event in case.incident.events],
            "metadata": case.incident.metadata,
        },
        ensure_ascii=False,
    ).lower()
    assert scenario.lower() not in visible_identity
    if case.truth.root_cause_code:
        assert case.truth.root_cause_code.lower() not in visible_identity


def test_anomaly_gate_skips_normal_incident() -> None:
    normal = build_scenario("normal")
    assessment = AnomalyDetector().assess(normal.incident)
    report = IncidentManager().diagnose(normal.incident, mode="multi")
    assert assessment.is_anomaly is False
    assert report.is_anomaly is False
    assert report.tool_calls == 0
    assert report.root_cause_code is None


def test_unseen_critical_template_reaches_unknown_diagnosis() -> None:
    base = build_scenario("normal").incident
    unseen = base.events[0].model_copy(
        update={
            "level": "CRITICAL",
            "template_id": "UNSEEN_KERNEL_FAILURE",
            "message": "Previously unseen fatal subsystem failure",
        }
    )
    incident = base.model_copy(update={"events": [unseen, *base.events[1:]]})
    assessment = AnomalyDetector().assess(incident)
    report = IncidentManager().diagnose(incident, mode="single")
    assert assessment.is_anomaly is True
    assert report.is_anomaly is True
    assert report.root_cause_code is None
    assert report.root_cause == "未识别的异常"
    assert report.severity.value == "CRITICAL"


@pytest.mark.parametrize("level", ["FATAL", "SEVERE"])
def test_fatal_and_severe_events_are_not_lost_before_diagnosis(level: str) -> None:
    base = build_scenario("normal").incident
    disk_failure = base.events[0].model_copy(
        update={
            "level": level,
            "template_id": "OPAQUE-TEMPLATE",
            "message": "Write syscall failed with ENOSPC on volume /data",
            "event_template": "Write syscall failed with ENOSPC on volume <*> ",
        }
    )
    incident = base.model_copy(update={"events": [disk_failure, *base.events[1:]]})

    report = IncidentManager().diagnose(incident, mode="multi")

    assert report.is_anomaly is True
    assert report.root_cause_code == "disk_full"
    assert report.severity.value == "CRITICAL"
    assert report.evidence[0].line_id == disk_failure.line_id


@pytest.mark.parametrize("mode", ["single", "multi"])
@pytest.mark.parametrize("scenario", SCENARIOS[1:])
def test_all_faults_have_correct_grounded_root_cause(scenario: str, mode: str) -> None:
    case = build_scenario(scenario)
    report = IncidentManager().diagnose(case.incident, mode=mode)
    valid_ids = {event.line_id for event in case.incident.events}
    assert report.is_anomaly is True
    assert report.root_cause_code == case.truth.root_cause_code
    assert report.evidence
    assert {item.line_id for item in report.evidence} <= valid_ids


def test_auditor_trades_more_calls_for_cleaner_evidence() -> None:
    case = build_scenario("db_pool_exhausted")
    manager = IncidentManager()
    single = manager.diagnose(case.incident, mode="single")
    multi = manager.diagnose(case.incident, mode="multi")
    truth = set(case.truth.evidence_ids)
    single_precision = len({item.line_id for item in single.evidence} & truth) / len(single.evidence)
    multi_precision = len({item.line_id for item in multi.evidence} & truth) / len(multi.evidence)
    assert multi_precision > single_precision
    assert multi.tool_calls > single.tool_calls


def test_trace_contains_actions_not_private_chain_of_thought(tmp_path) -> None:
    case = build_scenario("redis_unavailable")
    report = IncidentManager().diagnose(case.incident, mode="multi", trace_dir=tmp_path)
    lines = [
        json.loads(line)
        for line in Path(report.trace_path).read_text(encoding="utf-8").splitlines()
    ]
    event_types = {line["event_type"] for line in lines}
    assert {"anomaly_gate", "tool_call", "tool_result", "agent_final", "report_grounded"} <= event_types
    assert all("chain_of_thought" not in json.dumps(line) for line in lines)


def test_benchmark_quantifies_single_multi_tradeoff() -> None:
    result = compare_modes()
    single = result["single"]["metrics"]
    multi = result["multi"]["metrics"]
    assert single["root_cause_accuracy"] == 1.0
    assert multi["root_cause_accuracy"] == 1.0
    assert multi["evidence_precision"] > single["evidence_precision"]
    assert multi["average_tool_calls"] > single["average_tool_calls"]
