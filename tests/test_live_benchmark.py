from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from logrca.blind_data import build_blind_dataset
from logrca.live_benchmark import run_live_benchmark
from logrca.models import DiagnosisReport, Incident, Severity


def _report(incident: Incident, mode: str, root: str | None = None) -> DiagnosisReport:
    return DiagnosisReport(
        incident_id=incident.incident_id,
        mode=mode,  # type: ignore[arg-type]
        is_anomaly=root is not None,
        anomaly_score=0.8 if root else 0.1,
        severity=Severity.ERROR if root else Severity.INFO,
        root_cause_code=root,
        root_cause=root,
        confidence=0.8,
        tool_calls=2,
    )


class _FakeLiveRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        incident: Incident,
        *,
        model: str,
        mode: str,
        max_turns: int,
    ) -> SimpleNamespace:
        visible = json.dumps(incident.model_dump(mode="json"), ensure_ascii=False).lower()
        assert "root_cause_code" not in visible
        assert "evidence_ids" not in visible
        assert "fault_family" not in visible
        self.calls.append(
            {
                "case_id": incident.incident_id,
                "model": model,
                "mode": mode,
                "max_turns": max_turns,
            }
        )
        return SimpleNamespace(
            report=_report(incident, mode),
            input_tokens=100,
            output_tokens=50,
            requests=2,
            latency_ms=12.5,
        )


def test_live_benchmark_stratifies_repeats_prices_and_never_passes_truth() -> None:
    artifacts = build_blind_dataset(case_count=100, seed=701)
    fake = _FakeLiveRunner()
    result = run_live_benchmark(
        artifacts,
        model="fake-model",
        mode="both",
        split="all",
        max_cases=20,
        repeats=2,
        max_turns=7,
        input_price_per_million=2.0,
        output_price_per_million=8.0,
        bootstrap_iterations=0,
        runner=fake,
    )

    assert result["config"]["selected_cases"] == 20
    assert result["config"]["runner_received_truth"] is False
    assert len(fake.calls) == 20 * 2 * 2
    assert {call["model"] for call in fake.calls} == {"fake-model"}
    assert {call["max_turns"] for call in fake.calls} == {7}
    family_sizes = list(result["sample"]["family_counts"].values())
    assert len(family_sizes) == 10
    assert max(family_sizes) - min(family_sizes) <= 1

    for mode in ("single", "multi"):
        payload = result["modes"][mode]
        assert len(payload["runs"]) == 40
        assert len(payload["repeat_evaluations"]) == 2
        assert all(item["evaluation"]["metrics"]["cases"] == 20 for item in payload["repeat_evaluations"])
        assert payload["aggregate"]["total_requests"] == 80
        assert payload["aggregate"]["total_tokens"] == 6000
        assert payload["aggregate"]["total_cost_usd"] == pytest.approx(0.024)
        assert payload["aggregate"]["total_failure_rate"] == 0.0
        assert payload["root_stability"]["all_cases"]["root_consistency_rate"] == 1.0
        first_run = payload["runs"][0]
        assert first_run["report"] is not None
        assert first_run["input_tokens"] == 100
        assert first_run["output_tokens"] == 50
        assert first_run["cost_usd"] == pytest.approx(0.0006)

    assert result["aggregate"]["attempts"] == 80
    assert result["aggregate"]["total_cost_usd"] == pytest.approx(0.048)
    json.dumps(result, allow_nan=False)


def test_family_sample_is_deterministic() -> None:
    artifacts = build_blind_dataset(case_count=100, seed=702)
    first = run_live_benchmark(
        artifacts,
        model="fake-model",
        mode="single",
        split="all",
        max_cases=17,
        repeats=1,
        bootstrap_iterations=0,
        runner=_FakeLiveRunner(),
    )
    second = run_live_benchmark(
        artifacts,
        model="fake-model",
        mode="single",
        split="all",
        max_cases=17,
        repeats=1,
        bootstrap_iterations=0,
        runner=_FakeLiveRunner(),
    )
    assert first["sample"] == second["sample"]


class _ChangingRootRunner:
    def __init__(self, cases_per_repeat: int) -> None:
        self.calls = 0
        self.cases_per_repeat = cases_per_repeat

    def __call__(self, incident: Incident, *, model: str, mode: str, max_turns: int):
        repeat_index = self.calls // self.cases_per_repeat
        self.calls += 1
        root = "db_pool_exhausted" if repeat_index == 0 else "redis_unavailable"
        return SimpleNamespace(
            report=_report(incident, mode, root),
            input_tokens=10,
            output_tokens=5,
            requests=1,
            latency_ms=1.0,
        )


def test_root_consistency_detects_repeat_instability() -> None:
    artifacts = build_blind_dataset(case_count=100, seed=703)
    result = run_live_benchmark(
        artifacts,
        model="fake-model",
        mode="single",
        split="all",
        max_cases=5,
        repeats=2,
        bootstrap_iterations=0,
        runner=_ChangingRootRunner(cases_per_repeat=5),
    )
    stability = result["modes"]["single"]["root_stability"]["all_cases"]
    assert stability["consistent_cases"] == 0
    assert stability["root_consistency_rate"] == 0.0
    assert stability["mean_majority_root_share"] == 0.5
    assert len(stability["inconsistent_examples"]) == 5


class StructuredOutputExplosion(Exception):
    pass


class _FailingRunner(_FakeLiveRunner):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def __call__(self, incident: Incident, *, model: str, mode: str, max_turns: int):
        attempt = self.attempts
        self.attempts += 1
        if attempt == 0:
            raise StructuredOutputExplosion("structured output schema mismatch")
        if attempt == 1:
            raise TimeoutError("provider request timed out")
        return super().__call__(
            incident,
            model=model,
            mode=mode,
            max_turns=max_turns,
        )


def test_failures_are_recorded_scored_and_classified() -> None:
    artifacts = build_blind_dataset(case_count=100, seed=704)
    result = run_live_benchmark(
        artifacts,
        model="fake-model",
        mode="multi",
        split="all",
        max_cases=5,
        repeats=2,
        bootstrap_iterations=0,
        runner=_FailingRunner(),
    )
    payload = result["modes"]["multi"]
    aggregate = payload["aggregate"]
    assert aggregate["attempts"] == 10
    assert aggregate["successful_runs"] == 8
    assert aggregate["structured_failures"] == 1
    assert aggregate["runtime_failures"] == 1
    assert aggregate["structured_failure_rate"] == 0.1
    assert aggregate["runtime_failure_rate"] == 0.1
    assert payload["runs"][0]["error"]["kind"] == "structured"
    assert payload["runs"][1]["error"]["kind"] == "runtime"
    assert payload["runs"][0]["report"] is None
    assert all(
        item["evaluation"]["metrics"]["cases"] == 5
        for item in payload["repeat_evaluations"]
    )


def test_missing_api_key_fails_before_real_runner(monkeypatch) -> None:
    artifacts = build_blind_dataset(case_count=100, seed=705)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY.*未配置"):
        run_live_benchmark(
            artifacts,
            model="some-live-model",
            max_cases=1,
            repeats=1,
            bootstrap_iterations=0,
        )


@pytest.mark.parametrize(
    "options",
    [
        {"model": ""},
        {"model": "fake", "mode": "bad"},
        {"model": "fake", "split": "bad"},
        {"model": "fake", "max_cases": 0},
        {"model": "fake", "repeats": 0},
        {"model": "fake", "max_turns": 0},
        {"model": "fake", "input_price_per_million": 1.0},
        {
            "model": "fake",
            "input_price_per_million": -1.0,
            "output_price_per_million": 1.0,
        },
    ],
)
def test_live_benchmark_validates_configuration(options: dict[str, object]) -> None:
    artifacts = build_blind_dataset(case_count=100, seed=706)
    with pytest.raises(ValueError):
        run_live_benchmark(
            artifacts,
            bootstrap_iterations=0,
            runner=_FakeLiveRunner(),
            **options,  # type: ignore[arg-type]
        )

