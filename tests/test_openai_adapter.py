from __future__ import annotations

import pytest

from logrca.detector import AnomalyDetector
from logrca.models import AuditResult, EvidenceRef, Hypothesis, HypothesisBundle
from logrca.openai_adapter import compose_audited_report
from logrca.openai_adapter import run_openai_agents_with_telemetry, sdk_available
from logrca.synthetic import build_scenario


def test_live_path_composition_enforces_audit_candidate_and_valid_id_intersection() -> None:
    incident = build_scenario("db_pool_exhausted").incident
    direct_id = next(
        event.line_id for event in incident.events if event.template_id == "DB_POOL_EXHAUSTED"
    )
    incident = incident.model_copy(
        update={
            "events": [
                event.model_copy(update={"message": event.message + " password=hunter2"})
                if event.line_id == direct_id
                else event
                for event in incident.events
            ]
        }
    )
    symptom_id = next(
        event.line_id for event in incident.events if event.template_id == "GATEWAY_5XX"
    )
    primary = Hypothesis(
        code="db_pool_exhausted",
        title="数据库连接池耗尽",
        score=0.9,
        reasoning="candidate",
        evidence=[
            EvidenceRef(line_id=direct_id, kind="direct", summary="model text", weight=0.9),
            EvidenceRef(line_id=symptom_id, kind="symptom", summary="model text", weight=0.2),
        ],
    )
    bundle = HypothesisBundle(
        incident_id="model-controlled-id",
        hypotheses=[primary],
        runbook_checks=["delete production database"],
        remediation=["unsafe model action"],
    )
    audit = AuditResult(
        accepted=[
            EvidenceRef(line_id=direct_id, kind="direct", summary="auditor text", weight=0.9),
            EvidenceRef(line_id=symptom_id, kind="symptom", summary="auditor text", weight=0.2),
            EvidenceRef(line_id="FABRICATED-ID", kind="direct", summary="fake", weight=0.9),
        ],
        rejected=[],
        confidence_multiplier=0.8,
    )
    report = compose_audited_report(
        incident,
        AnomalyDetector().assess(incident),
        bundle,
        audit,
        tool_calls=7,
    )
    assert report.incident_id == incident.incident_id
    assert [item.line_id for item in report.evidence] == [direct_id]
    assert report.evidence[0].summary.startswith("order-service:")
    assert "hunter2" not in report.evidence[0].summary
    assert "[REDACTED]" in report.evidence[0].summary
    assert "delete production database" not in report.next_checks
    assert "unsafe model action" not in report.remediation
    assert report.tool_calls == 7

    no_evidence = compose_audited_report(
        incident,
        AnomalyDetector().assess(incident),
        bundle,
        AuditResult(
            accepted=[],
            rejected=[],
            confidence_multiplier=1.0,
        ),
    )
    assert no_evidence.evidence == []
    assert no_evidence.confidence <= 0.35


def test_live_composition_handles_grounded_evidence_after_first_100_events() -> None:
    base = build_scenario("db_pool_exhausted").incident
    seed_event = base.events[0]
    events = [
        seed_event.model_copy(
            update={
                "line_id": f"L-{index:03d}",
                "message": f"ordinary event {index}",
                "event_template": "ordinary event <*>",
            }
        )
        for index in range(150)
    ]
    events[144] = events[144].model_copy(
        update={"message": "root evidence access_token=secret-value"}
    )
    incident = base.model_copy(update={"events": events})
    reference = EvidenceRef(
        line_id="L-144", kind="direct", summary="untrusted model text", weight=0.9
    )
    bundle = HypothesisBundle(
        incident_id=incident.incident_id,
        hypotheses=[
            Hypothesis(
                code="db_pool_exhausted",
                title="数据库连接池耗尽",
                score=0.8,
                reasoning="candidate",
                evidence=[reference],
            )
        ],
    )

    report = compose_audited_report(
        incident,
        AnomalyDetector().assess(incident),
        bundle,
        AuditResult(accepted=[reference], rejected=[]),
    )

    assert [item.line_id for item in report.evidence] == ["L-144"]
    assert "secret-value" not in report.evidence[0].summary
    assert "[REDACTED]" in report.evidence[0].summary


def test_normal_incident_uses_gate_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    run = run_openai_agents_with_telemetry(
        build_scenario("normal").incident,
        model="explicit-test-model",
        mode="single",
    )
    assert run.report.is_anomaly is False
    assert run.requests == 0
    assert run.total_tokens == 0
    assert run.to_dict()["report"]["mode"] == "single"


def test_abnormal_live_run_refuses_to_fake_model_result_without_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if not sdk_available():
        return
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        run_openai_agents_with_telemetry(
            build_scenario("db_pool_exhausted").incident,
            model="explicit-test-model",
        )
