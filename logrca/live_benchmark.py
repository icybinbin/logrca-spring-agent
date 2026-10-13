"""Repeatable real-model benchmark over the blind RCA corpus.

The module owns experiment orchestration, not diagnosis.  Its injectable
``runner`` boundary makes every control-flow and accounting rule testable
without a network call; production use defaults to
``run_openai_agents_with_telemetry``.

Ground truth is used only for stratified case selection and post-run scoring.
The runner receives a deep-copied :class:`Incident` plus model configuration,
never a truth record or label.
"""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from typing import Any, Literal

from .benchmarking import GroundTruthRecord, PredictionRecord, evaluate_records
from .blind_data import BlindDatasetArtifacts, BlindTruthRecord, verify_manifest
from .models import DiagnosisReport, Incident
from .openai_adapter import run_openai_agents_with_telemetry


LiveBenchmarkMode = Literal["single", "multi", "both"]
LiveBenchmarkSplit = Literal["all", "train", "dev", "test"]
LiveRunner = Callable[..., Any]


class _StructuredResultError(ValueError):
    """The runner returned a value that violates the structured contract."""


def _validate_configuration(
    *,
    model: str,
    mode: str,
    split: str,
    max_cases: int | None,
    repeats: int,
    max_turns: int,
    input_price_per_million: float | None,
    output_price_per_million: float | None,
    bootstrap_iterations: int,
) -> None:
    if not model.strip():
        raise ValueError("model must be explicit and non-empty")
    if mode not in {"single", "multi", "both"}:
        raise ValueError("mode must be one of: single, multi, both")
    if split not in {"all", "train", "dev", "test"}:
        raise ValueError("split must be one of: all, train, dev, test")
    if max_cases is not None and max_cases <= 0:
        raise ValueError("max_cases must be positive when provided")
    if repeats <= 0:
        raise ValueError("repeats must be positive")
    if max_turns <= 0:
        raise ValueError("max_turns must be positive")
    if bootstrap_iterations < 0:
        raise ValueError("bootstrap_iterations must be non-negative")
    prices = (input_price_per_million, output_price_per_million)
    if (prices[0] is None) != (prices[1] is None):
        raise ValueError(
            "input_price_per_million and output_price_per_million must be "
            "provided together so total cost is not partial"
        )
    if any(value is not None and value < 0 for value in prices):
        raise ValueError("token prices must be non-negative")


def _eligible_incidents(
    artifacts: BlindDatasetArtifacts, split: LiveBenchmarkSplit
) -> list[Incident]:
    if split == "all":
        return list(artifacts.corpus.incidents)
    labels = artifacts.truth_sidecar.labels
    return [
        incident
        for incident in artifacts.corpus.incidents
        if labels[incident.incident_id].split == split
    ]


def _stratified_sample(
    artifacts: BlindDatasetArtifacts,
    *,
    split: LiveBenchmarkSplit,
    max_cases: int | None,
) -> list[Incident]:
    """Select a deterministic, approximately balanced sample by fault family."""

    eligible = _eligible_incidents(artifacts, split)
    if not eligible:
        raise ValueError(f"split {split!r} contains no incidents")
    labels = artifacts.truth_sidecar.labels
    buckets: dict[str, list[Incident]] = defaultdict(list)
    for incident in eligible:
        buckets[labels[incident.incident_id].fault_family].append(incident)

    # Each family has its own shuffle so adding cases to one family does not
    # reorder all other families in a reproduced experiment.
    for family, incidents in buckets.items():
        incidents.sort(key=lambda item: item.incident_id)
        random.Random(f"{artifacts.manifest.seed}|{split}|{family}").shuffle(incidents)
    families = sorted(buckets)
    random.Random(f"{artifacts.manifest.seed}|{split}|family-order").shuffle(families)

    target = len(eligible) if max_cases is None else min(max_cases, len(eligible))
    selected: list[Incident] = []
    offsets = {family: 0 for family in families}
    while len(selected) < target:
        made_progress = False
        for family in families:
            offset = offsets[family]
            if offset >= len(buckets[family]):
                continue
            selected.append(buckets[family][offset])
            offsets[family] += 1
            made_progress = True
            if len(selected) == target:
                break
        if not made_progress:  # defensive: target can never exceed eligible
            break
    return selected


def _difficulty(label: BlindTruthRecord) -> str:
    disruptive = {"missing_direct", "missing_evidence", "wrong_trace"}
    perturbations = set(label.perturbations)
    if len(perturbations) >= 2 or perturbations.intersection(disruptive):
        return "hard"
    if perturbations:
        return "medium"
    return "easy"


def _truths_after_execution(
    incidents: Sequence[Incident], artifacts: BlindDatasetArtifacts
) -> list[GroundTruthRecord]:
    records: list[GroundTruthRecord] = []
    for incident in incidents:
        label = artifacts.truth_sidecar.labels[incident.incident_id]
        records.append(
            GroundTruthRecord.from_ground_truth(
                label,
                case_id=incident.incident_id,
                fault_family=label.fault_family,
                difficulty=_difficulty(label),
                is_unknown=label.is_anomaly and label.root_cause_code is None,
                available_evidence_ids=(event.line_id for event in incident.events),
                metadata={
                    "split": label.split,
                    "group_id": label.group_id,
                    "perturbations": list(label.perturbations),
                    "trace_corruption_role": label.trace_corruption_role,
                },
            )
        )
    return records


def _cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_price_per_million: float | None,
    output_price_per_million: float | None,
) -> float | None:
    if input_price_per_million is None or output_price_per_million is None:
        return None
    return (
        input_tokens * input_price_per_million
        + output_tokens * output_price_per_million
    ) / 1_000_000.0


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    converted = int(value)
    if converted < 0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _non_negative_float(value: Any, name: str) -> float:
    converted = float(value)
    if converted < 0:
        raise ValueError(f"{name} must be non-negative")
    return converted


def _structured_report(value: Any, incident_id: str, mode: str) -> DiagnosisReport:
    try:
        report = DiagnosisReport.model_validate(value)
    except Exception as exc:
        raise _StructuredResultError(f"invalid DiagnosisReport: {exc}") from exc
    if report.incident_id != incident_id:
        raise _StructuredResultError(
            f"report incident_id mismatch: expected {incident_id}, got {report.incident_id}"
        )
    if report.mode != mode:
        raise _StructuredResultError(
            f"report mode mismatch: expected {mode}, got {report.mode}"
        )
    return report


def _error_kind(exc: Exception) -> Literal["structured", "runtime"]:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    structured_hints = (
        "structured",
        "validation",
        "schema",
        "modelbehavior",
        "final_output",
        "diagnosisreport",
    )
    if isinstance(exc, _StructuredResultError) or any(
        hint in name or hint in message for hint in structured_hints
    ):
        return "structured"
    return "runtime"


def _execute_attempt(
    *,
    runner: LiveRunner,
    incident: Incident,
    model: str,
    mode: Literal["single", "multi"],
    repeat: int,
    max_turns: int,
    input_price_per_million: float | None,
    output_price_per_million: float | None,
) -> tuple[dict[str, Any], PredictionRecord]:
    """Run one model call and convert any failure into a scored abstention."""

    started = time.perf_counter()
    try:
        # This is the complete runner boundary.  No label, family, expected
        # evidence or other evaluation-only value appears in the call.
        live = runner(
            incident.model_copy(deep=True),
            model=model,
            mode=mode,
            max_turns=max_turns,
        )
        report = _structured_report(getattr(live, "report", None), incident.incident_id, mode)
        input_tokens = _non_negative_int(getattr(live, "input_tokens"), "input_tokens")
        output_tokens = _non_negative_int(getattr(live, "output_tokens"), "output_tokens")
        requests = _non_negative_int(getattr(live, "requests"), "requests")
        latency_ms = _non_negative_float(getattr(live, "latency_ms"), "latency_ms")
        cost = _cost_usd(
            input_tokens,
            output_tokens,
            input_price_per_million,
            output_price_per_million,
        )
        prediction = PredictionRecord.from_diagnosis_report(
            report,
            case_id=incident.incident_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            metadata={"requests": requests, "repeat": repeat, "mode": mode},
        )
        record = {
            "case_id": incident.incident_id,
            "mode": mode,
            "repeat": repeat,
            "status": "ok",
            "report": report.model_dump(mode="json"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "requests": requests,
            "latency_ms": latency_ms,
            "cost_usd": cost,
            "error": None,
        }
        return record, prediction
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000.0
        kind = _error_kind(exc)
        record = {
            "case_id": incident.incident_id,
            "mode": mode,
            "repeat": repeat,
            "status": "error",
            "report": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "requests": None,
            "latency_ms": latency_ms,
            "cost_usd": None,
            "error": {
                "kind": kind,
                "type": type(exc).__name__,
                "message": str(exc)[:500],
            },
        }
        # Do not drop failures: the no-answer prediction remains in that
        # repeat's denominator, while failure rates disclose why it occurred.
        prediction = PredictionRecord(
            case_id=incident.incident_id,
            is_anomaly=False,
            abstained=True,
            latency_ms=latency_ms,
            metadata={"error_kind": kind, "repeat": repeat, "mode": mode},
        )
        return record, prediction


def _root_label(run: dict[str, Any]) -> str | None:
    if run["status"] != "ok":
        return None
    report = run["report"]
    return str(report["root_cause_code"] or "__ABSTAIN__")


def _stability_for_ids(
    case_ids: Sequence[str],
    runs: Sequence[dict[str, Any]],
    *,
    repeats: int,
    example_limit: int = 20,
) -> dict[str, Any]:
    roots: dict[str, list[str | None]] = {case_id: [] for case_id in case_ids}
    for run in runs:
        if run["case_id"] in roots:
            roots[run["case_id"]].append(_root_label(run))

    consistent = 0
    majority_shares: list[float] = []
    inconsistent_examples: list[dict[str, Any]] = []
    for case_id in case_ids:
        labels = roots[case_id]
        successes = [label for label in labels if label is not None]
        fully_successful = len(successes) == repeats
        is_consistent = fully_successful and len(set(successes)) == 1
        consistent += is_consistent
        if successes:
            majority_shares.append(max(Counter(successes).values()) / len(successes))
        if not is_consistent and len(inconsistent_examples) < example_limit:
            inconsistent_examples.append(
                {
                    "case_id": case_id,
                    "root_by_repeat": labels,
                    "successful_repeats": len(successes),
                }
            )
    return {
        "cases": len(case_ids),
        "repeats": repeats,
        "consistent_cases": consistent,
        "root_consistency_rate": consistent / len(case_ids) if case_ids else 0.0,
        "mean_majority_root_share": (
            sum(majority_shares) / len(majority_shares) if majority_shares else 0.0
        ),
        "inconsistent_examples": inconsistent_examples,
        "example_limit": example_limit,
        "definition": (
            "A case is consistent only when every repeat succeeds and all repeats "
            "return the same root code (or all abstain)."
        ),
    }


def _aggregate_runs(
    runs: Sequence[dict[str, Any]], *, pricing_enabled: bool
) -> dict[str, Any]:
    attempts = len(runs)
    successes = sum(run["status"] == "ok" for run in runs)
    structured = sum(
        run["status"] == "error" and run["error"]["kind"] == "structured"
        for run in runs
    )
    runtime = sum(
        run["status"] == "error" and run["error"]["kind"] == "runtime"
        for run in runs
    )
    successful_runs = [run for run in runs if run["status"] == "ok"]
    return {
        "attempts": attempts,
        "successful_runs": successes,
        "structured_failures": structured,
        "runtime_failures": runtime,
        "structured_failure_rate": structured / attempts if attempts else 0.0,
        "runtime_failure_rate": runtime / attempts if attempts else 0.0,
        "total_failure_rate": (structured + runtime) / attempts if attempts else 0.0,
        "total_input_tokens": sum(run["input_tokens"] for run in successful_runs),
        "total_output_tokens": sum(run["output_tokens"] for run in successful_runs),
        "total_tokens": sum(run["total_tokens"] for run in successful_runs),
        "total_requests": sum(run["requests"] for run in successful_runs),
        "total_latency_ms": sum(float(run["latency_ms"]) for run in runs),
        "total_cost_usd": (
            sum(float(run["cost_usd"]) for run in successful_runs)
            if pricing_enabled
            else None
        ),
        "cost_scope_note": (
            "Cost covers successful runs with returned token telemetry; a provider "
            "may still bill requests that failed before telemetry was returned."
            if pricing_enabled
            else "No price was supplied; token usage is reported but cost is null."
        ),
    }


def run_live_benchmark(
    artifacts: BlindDatasetArtifacts,
    *,
    model: str,
    mode: LiveBenchmarkMode = "both",
    split: LiveBenchmarkSplit = "test",
    max_cases: int | None = 30,
    repeats: int = 3,
    max_turns: int = 12,
    input_price_per_million: float | None = None,
    output_price_per_million: float | None = None,
    bootstrap_iterations: int = 1000,
    runner: LiveRunner | None = None,
) -> dict[str, Any]:
    """Run repeated live-model evaluations on a deterministic family sample.

    Supplying ``runner`` is the only offline-test path.  With the production
    runner (the default), a missing ``OPENAI_API_KEY`` fails before hashing the
    dataset or attempting a model request.
    """

    _validate_configuration(
        model=model,
        mode=mode,
        split=split,
        max_cases=max_cases,
        repeats=repeats,
        max_turns=max_turns,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        bootstrap_iterations=bootstrap_iterations,
    )
    if runner is None:
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError(
                "OPENAI_API_KEY 未配置，真实模型基准未启动。请先设置环境变量；"
                "离线单元测试应显式注入 fake runner。"
            )
        active_runner = run_openai_agents_with_telemetry
    else:
        active_runner = runner

    if not verify_manifest(artifacts):
        raise ValueError("blind dataset manifest verification failed")
    incidents = _stratified_sample(artifacts, split=split, max_cases=max_cases)
    labels = artifacts.truth_sidecar.labels
    family_counts = dict(
        sorted(Counter(labels[item.incident_id].fault_family for item in incidents).items())
    )
    active_modes: tuple[Literal["single", "multi"], ...] = (
        ("single", "multi") if mode == "both" else (mode,)  # type: ignore[assignment]
    )

    # All model executions finish before GroundTruthRecord objects are created.
    # Sampling reads family/split labels, but the runner boundary below receives
    # only Incident + model/mode/max_turns.
    runs_by_mode: dict[str, list[dict[str, Any]]] = {}
    predictions_by_mode_repeat: dict[str, list[list[PredictionRecord]]] = {}
    for active_mode in active_modes:
        mode_runs: list[dict[str, Any]] = []
        repeat_predictions: list[list[PredictionRecord]] = []
        for repeat in range(1, repeats + 1):
            predictions: list[PredictionRecord] = []
            for incident in incidents:
                run, prediction = _execute_attempt(
                    runner=active_runner,
                    incident=incident,
                    model=model,
                    mode=active_mode,
                    repeat=repeat,
                    max_turns=max_turns,
                    input_price_per_million=input_price_per_million,
                    output_price_per_million=output_price_per_million,
                )
                mode_runs.append(run)
                predictions.append(prediction)
            repeat_predictions.append(predictions)
        runs_by_mode[active_mode] = mode_runs
        predictions_by_mode_repeat[active_mode] = repeat_predictions

    truths = _truths_after_execution(incidents, artifacts)
    known_fault_ids = [
        truth.case_id
        for truth in truths
        if truth.is_anomaly and not truth.is_unknown and truth.root_cause_code
    ]
    all_case_ids = [incident.incident_id for incident in incidents]
    pricing_enabled = input_price_per_million is not None
    mode_results: dict[str, Any] = {}
    for active_mode in active_modes:
        repeat_results = []
        for repeat_index, predictions in enumerate(
            predictions_by_mode_repeat[active_mode], start=1
        ):
            evaluation = evaluate_records(
                truths,
                predictions,
                bootstrap_iterations=bootstrap_iterations,
                seed=artifacts.manifest.seed + repeat_index,
            )
            repeat_results.append(
                {"repeat": repeat_index, "evaluation": evaluation.to_dict()}
            )
        mode_runs = runs_by_mode[active_mode]
        mode_results[active_mode] = {
            "runs": mode_runs,
            "repeat_evaluations": repeat_results,
            "aggregate": _aggregate_runs(mode_runs, pricing_enabled=pricing_enabled),
            "root_stability": {
                "all_cases": _stability_for_ids(
                    all_case_ids, mode_runs, repeats=repeats
                ),
                "known_fault_cases": _stability_for_ids(
                    known_fault_ids, mode_runs, repeats=repeats
                ),
            },
        }

    all_runs = [run for mode_runs in runs_by_mode.values() for run in mode_runs]
    result = {
        "benchmark": "openai-live-blind-log-rca",
        "dataset_manifest": artifacts.manifest.model_dump(mode="json"),
        "config": {
            "model": model,
            "mode": mode,
            "split": split,
            "max_cases": max_cases,
            "selected_cases": len(incidents),
            "repeats": repeats,
            "max_turns": max_turns,
            "bootstrap_iterations": bootstrap_iterations,
            "input_price_per_million": input_price_per_million,
            "output_price_per_million": output_price_per_million,
            "runner_received_truth": False,
        },
        "sample": {
            "case_ids": all_case_ids,
            "family_counts": family_counts,
            "sampling": "deterministic balanced round-robin by fault_family",
        },
        "modes": mode_results,
        "aggregate": _aggregate_runs(all_runs, pricing_enabled=pricing_enabled),
    }
    # Assert the public contract here so future additions cannot accidentally
    # introduce a Path, model object, or NaN that breaks artifact export.
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


__all__ = [
    "LiveBenchmarkMode",
    "LiveBenchmarkSplit",
    "LiveRunner",
    "run_live_benchmark",
]
