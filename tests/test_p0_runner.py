from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from logrca.agents import IncidentManager
from logrca.models import Incident, LogEvent
from logrca.p0_runner import run_p0_benchmark


def test_runner_executes_100_blind_cases_in_both_modes() -> None:
    result = run_p0_benchmark(
        case_count=100,
        seed=77,
        mode="both",
        split="all",
        failure_limit=3,
        bootstrap_iterations=40,
    )

    assert result["dataset_manifest"]["case_count"] == 100
    assert result["run_config"]["selected_cases"] == 100
    assert result["run_config"]["manager_received_truth"] is False
    assert set(result["modes"]) == {"single", "multi"}
    for mode_result in result["modes"].values():
        evaluation = mode_result["evaluation"]
        metrics = evaluation["metrics"]
        assert metrics["cases"] == 100
        assert metrics["efficiency"]["latency_cases"] == 100
        assert metrics["efficiency"]["tool_call_cases"] == 100
        assert evaluation["confidence_intervals"]["detection.f1"]["iterations"] == 40
        failures = mode_result["case_failures"]
        assert set(failures["counts"]) == {
            "misclassification",
            "wrong_root_cause",
            "incomplete_evidence",
            "execution_error",
        }
        assert all(len(items) <= 3 for items in failures["examples"].values())
        assert failures["counts"]["execution_error"] == 0
        direct_rca = mode_result["visible_direct_evidence_rca"]
        assert direct_rca["eligible_cases"] > 0
        assert 0.0 <= direct_rca["top1_accuracy"] <= 1.0
        assert direct_rca["excluded_missing_direct_cases"] > 0
        counterfactual = mode_result["counterfactual_safety"]
        assert counterfactual["missing_direct_cases"] > 0
        assert 0.0 <= counterfactual["abstention_rate"] <= 1.0
        assert len(counterfactual["cases"]) == counterfactual["missing_direct_cases"]
        correlation = mode_result["correlation_resilience"]
        assert correlation["eligible_trace_corruption_cases"] > 0
        assert correlation["event_conservation_rate"] == 1.0
        assert correlation["diagnosis_error_count"] == 0
        assert 0.0 <= correlation[
            "known_root_with_direct_evidence_recovery_rate"
        ] <= 1.0
        assert correlation["trace_corruption_role_metrics"]
        for role, role_metric in correlation[
            "trace_corruption_role_metrics"
        ].items():
            assert role_metric["cases"] == correlation[
                "trace_corruption_role_counts"
            ][role]
            assert 0.0 <= role_metric["recovery_rate"] <= 1.0
            assert 0.0 <= role_metric["known_root_recovery_rate"] <= 1.0

    ablation = result["single_vs_multi_ablation"]
    assert ablation["baseline"] == "single"
    assert set(ablation["variant_metrics"]) == {"single", "multi"}
    assert "multi" in ablation["deltas_vs_baseline"]
    assert "efficiency.average_tool_calls" in ablation["deltas_vs_baseline"]["multi"]


class _VisibleInputSpy:
    def __init__(self) -> None:
        self.delegate = IncidentManager()
        self.calls: list[tuple[str, str]] = []

    def diagnose(self, incident: Incident, mode: str):
        visible = json.dumps(incident.model_dump(mode="json"), ensure_ascii=False).lower()
        assert "root_cause_code" not in visible
        assert "evidence_ids" not in visible
        assert "fault_family" not in visible
        self.calls.append((incident.incident_id, mode))
        return self.delegate.diagnose(incident, mode=mode)  # type: ignore[arg-type]


def test_split_selection_and_manager_truth_boundary() -> None:
    spy = _VisibleInputSpy()
    result = run_p0_benchmark(
        case_count=100,
        seed=31,
        split="test",
        mode="single",
        manager=spy,  # type: ignore[arg-type]
        bootstrap_iterations=0,
    )

    expected = result["dataset_manifest"]["split_counts"]["test"]
    assert expected > 0
    assert result["run_config"]["selected_cases"] == expected
    # Base diagnosis calls each selected case once; the correlation resilience
    # suite additionally diagnoses every sub-incident created from corrupted
    # trace IDs through the same truth-free manager boundary.
    assert len(spy.calls) >= expected
    assert {mode for _, mode in spy.calls} == {"single"}
    assert set(result["modes"]) == {"single"}
    assert result["single_vs_multi_ablation"] is None


def test_export_writes_separate_blind_artifacts_and_result(tmp_path) -> None:
    background = LogEvent(
        line_id="private-public-dataset-id",
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        service="background-service",
        level="INFO",
        message="Distinct public background message",
        template_id="PRIVATE_LABEL_TEMPLATE",
        event_template="Distinct public background message",
        source="private-source-label",
        metadata={"anomaly": True},
    )
    result = run_p0_benchmark(
        case_count=100,
        seed=49,
        mode="single",
        background_events=[background],
        bootstrap_iterations=0,
        export_dir=tmp_path,
    )

    assert set(result["exported_files"]) == {
        "corpus",
        "truth",
        "manifest",
        "result",
    }
    corpus = (tmp_path / "incidents.agent.jsonl").read_text(encoding="utf-8")
    truth = (tmp_path / "truth.sidecar.json").read_text(encoding="utf-8")
    saved_result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "Distinct public background message" in corpus
    assert "private-public-dataset-id" not in corpus
    assert "PRIVATE_LABEL_TEMPLATE" not in corpus
    assert "root_cause_code" not in corpus
    assert "root_cause_code" in truth
    assert saved_result == result


@pytest.mark.parametrize(
    ("argument", "value", "message"),
    [
        ("split", "invalid", "split must be"),
        ("mode", "invalid", "mode must be"),
        ("failure_limit", -1, "non-negative"),
        ("bootstrap_iterations", -1, "non-negative"),
    ],
)
def test_runner_validates_configuration(argument: str, value: object, message: str) -> None:
    options = {argument: value}
    with pytest.raises(ValueError, match=message):
        run_p0_benchmark(case_count=100, **options)  # type: ignore[arg-type]
