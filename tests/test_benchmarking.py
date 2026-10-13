from __future__ import annotations

import json

import pytest

from logrca.agents import IncidentManager
from logrca.benchmarking import (
    GroundTruthRecord,
    PredictionRecord,
    compare_ablations,
    evaluate_records,
)
from logrca.synthetic import build_scenario


def _records() -> tuple[list[GroundTruthRecord], list[PredictionRecord]]:
    truths = [
        GroundTruthRecord(
            "known-a",
            True,
            "ROOT_A",
            ("e1", "e2"),
            fault_family="database",
            difficulty="hard",
            available_evidence_ids=("e1", "e2", "e3"),
        ),
        GroundTruthRecord(
            "known-b",
            True,
            "ROOT_B",
            ("e4",),
            fault_family="cache",
            difficulty="hard",
            available_evidence_ids=("e4", "e5"),
        ),
        GroundTruthRecord(
            "unknown",
            True,
            None,
            ("e6",),
            is_unknown=True,
            fault_family="novel",
            difficulty="ood",
            available_evidence_ids=("e6",),
        ),
        GroundTruthRecord(
            "normal",
            False,
            None,
            (),
            fault_family="normal",
            difficulty="easy",
            available_evidence_ids=("e7",),
        ),
    ]
    predictions = [
        PredictionRecord(
            "known-a",
            True,
            ("ROOT_A", "ROOT_Z"),
            ("e1", "e3"),
            tool_calls=4,
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.010,
            latency_ms=100,
        ),
        PredictionRecord(
            "known-b",
            True,
            ("ROOT_X", "ROOT_C", "ROOT_B"),
            ("e4",),
            citation_ids=("e4", "ghost"),
            tool_calls=6,
            input_tokens=200,
            output_tokens=40,
            cost_usd=0.020,
            latency_ms=200,
        ),
        PredictionRecord(
            "unknown",
            True,
            (),
            ("e6",),
            abstained=True,
            unsupported_claim_count=1,
            claim_count=2,
            tool_calls=2,
            input_tokens=50,
            output_tokens=10,
            cost_usd=0.005,
            latency_ms=300,
        ),
        PredictionRecord(
            "normal",
            False,
            (),
            (),
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=400,
        ),
    ]
    return truths, predictions


def test_evaluate_records_covers_detection_rca_evidence_grounding_and_cost() -> None:
    truths, predictions = _records()
    result = evaluate_records(truths, predictions, bootstrap_iterations=200, seed=7)
    metrics = result.metrics

    assert metrics["detection"] == {
        "tp": 3,
        "fp": 0,
        "fn": 0,
        "tn": 1,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
    }
    assert metrics["rca"]["top1_accuracy"] == 0.5
    assert metrics["rca"]["top3_accuracy"] == 1.0
    assert metrics["rca"]["mrr"] == pytest.approx(2 / 3)
    assert metrics["unknown"]["abstention_rate"] == 1.0

    assert metrics["evidence"]["micro_precision"] == 0.75
    assert metrics["evidence"]["micro_recall"] == 0.75
    assert metrics["evidence"]["micro_f1"] == 0.75
    assert metrics["evidence"]["macro_f1"] == 0.875
    assert metrics["evidence"]["exact_set_accuracy"] == 0.75

    assert metrics["grounding"]["invalid_citations"] == 1
    assert metrics["grounding"]["invalid_citation_rate"] == 0.2
    assert metrics["grounding"]["unsupported_claim_rate"] == 0.5
    assert metrics["efficiency"]["average_tool_calls"] == 3.0
    assert metrics["efficiency"]["total_tokens"] == 420
    assert metrics["efficiency"]["total_cost_usd"] == pytest.approx(0.035)
    assert metrics["efficiency"]["p50_latency_ms"] == 250.0
    assert metrics["efficiency"]["p95_latency_ms"] == pytest.approx(385.0)

    assert set(result.strata["fault_family"]) == {
        "database",
        "cache",
        "novel",
        "normal",
    }
    assert set(result.strata["difficulty"]) == {"easy", "hard", "ood"}
    assert result.strata["difficulty"]["hard"]["cases"] == 2
    interval = result.confidence_intervals["rca.top1_accuracy"]
    assert interval["iterations"] == 200
    assert interval["lower"] <= interval["estimate"] <= interval["upper"]


def test_result_is_json_serializable_and_bootstrap_is_deterministic() -> None:
    truths, predictions = _records()
    first = evaluate_records(truths, predictions, bootstrap_iterations=50, seed=123)
    second = evaluate_records(truths, predictions, bootstrap_iterations=50, seed=123)
    assert first.confidence_intervals == second.confidence_intervals
    payload = json.loads(first.to_json())
    assert payload["metrics"]["cases"] == 4
    assert payload["config"]["bootstrap_iterations"] == 50
    assert payload["cases"][0]["case_id"] == "known-a"


def test_unannotated_evidence_is_excluded_but_annotated_empty_is_scored() -> None:
    truths = [
        GroundTruthRecord("not-labelled", False, evidence_ids=None),
        GroundTruthRecord("labelled-empty", False, evidence_ids=()),
    ]
    predictions = [
        PredictionRecord("not-labelled", False, evidence_ids=("anything",)),
        PredictionRecord("labelled-empty", False, evidence_ids=()),
    ]
    metrics = evaluate_records(truths, predictions, bootstrap_iterations=0).metrics
    assert metrics["evidence"]["eligible_cases"] == 1
    assert metrics["evidence"]["exact_set_accuracy"] == 1.0
    assert metrics["evidence"]["macro_f1"] == 1.0


def test_case_alignment_is_strict() -> None:
    with pytest.raises(ValueError, match="missing_predictions"):
        evaluate_records(
            [GroundTruthRecord("a", False), GroundTruthRecord("b", False)],
            [PredictionRecord("a", False)],
        )
    with pytest.raises(ValueError, match="duplicate prediction"):
        evaluate_records(
            [GroundTruthRecord("a", False)],
            [PredictionRecord("a", False), PredictionRecord("a", False)],
        )


def test_diagnosis_report_adapter_preserves_ranking_and_evidence() -> None:
    case = build_scenario("db_pool_exhausted")
    report = IncidentManager().diagnose(case.incident, mode="single")
    prediction = PredictionRecord.from_diagnosis_report(
        report,
        case_id=case.truth.scenario,
        input_tokens=123,
        output_tokens=45,
        latency_ms=10,
    )
    truth = GroundTruthRecord.from_ground_truth(
        case.truth,
        available_evidence_ids=(event.line_id for event in case.incident.events),
        fault_family="database",
        difficulty="easy",
    )
    result = evaluate_records([truth], [prediction], bootstrap_iterations=10)

    assert prediction.ranked_root_causes[0] == case.truth.root_cause_code
    assert set(prediction.evidence_ids) == {item.line_id for item in report.evidence}
    assert result.metrics["rca"]["top1_accuracy"] == 1.0
    assert result.metrics["grounding"]["invalid_citation_rate"] == 0.0
    assert result.metrics["efficiency"]["total_tokens"] == 168


def test_ablation_summary_reports_quality_gain_and_resource_tradeoff() -> None:
    truths = [
        GroundTruthRecord("a", True, "ROOT", ("e1",), available_evidence_ids=("e1",)),
        GroundTruthRecord("b", False, evidence_ids=(), available_evidence_ids=()),
    ]
    baseline = [
        PredictionRecord("a", True, ("WRONG",), (), tool_calls=1, latency_ms=10),
        PredictionRecord("b", False, (), (), tool_calls=0, latency_ms=10),
    ]
    audited = [
        PredictionRecord("a", True, ("ROOT",), ("e1",), tool_calls=3, latency_ms=20),
        PredictionRecord("b", False, (), (), tool_calls=0, latency_ms=20),
    ]
    comparison = compare_ablations(
        truths,
        {"baseline": baseline, "with_auditor": audited},
        baseline="baseline",
        bootstrap_iterations=0,
    )
    deltas = comparison["deltas_vs_baseline"]["with_auditor"]
    assert deltas["rca.top1_accuracy"]["improvement"] == 1.0
    assert deltas["evidence.micro_f1"]["improvement"] == 1.0
    assert deltas["efficiency.average_tool_calls"]["delta"] == 1.0
    assert deltas["efficiency.average_tool_calls"]["improvement"] == -1.0
    json.dumps(comparison, allow_nan=False)

