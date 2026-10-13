"""End-to-end offline P0 benchmark orchestration.

This runner is intentionally local and deployment-free.  It generates a
label-separated, developer-authored corpus, passes only Agent-visible ``Incident`` objects to
``IncidentManager``, then joins reports with the hidden truth sidecar in the
evaluation layer.  At no point is a truth record an argument to the manager.

The runner is useful both as an executable project acceptance test and as the
source of honest resume metrics: results include failures and uncertainty, not
only aggregate success rates.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from .agents import IncidentManager
from .benchmarking import (
    GroundTruthRecord,
    PredictionRecord,
    compare_ablations,
    evaluate_records,
)
from .blind_data import (
    DEFAULT_CASE_COUNT,
    DEFAULT_SEED,
    BlindDatasetArtifacts,
    BlindTruthRecord,
    build_blind_dataset,
    export_blind_dataset,
    incidents_for_split,
    verify_manifest,
)
from .correlation import build_incidents
from .models import Incident, LogEvent


BenchmarkSplit = Literal["all", "train", "dev", "test"]
BenchmarkMode = Literal["single", "multi", "both"]


def _difficulty(label: BlindTruthRecord) -> str:
    """Derive an auditable difficulty bucket from visible perturbation policy."""

    disruptive = {"missing_direct", "missing_evidence", "wrong_trace"}
    perturbations = set(label.perturbations)
    if len(perturbations) >= 2 or perturbations.intersection(disruptive):
        return "hard"
    if perturbations:
        return "medium"
    return "easy"


def _selected_incidents(
    artifacts: BlindDatasetArtifacts, split: BenchmarkSplit
) -> list[Incident]:
    if split == "all":
        # A fresh copy makes it impossible for a stateful/custom manager to
        # mutate the canonical corpus later exported for reproduction.
        return [item.model_copy(deep=True) for item in artifacts.corpus.incidents]
    return incidents_for_split(artifacts, split)


def _truth_record(incident: Incident, label: BlindTruthRecord) -> GroundTruthRecord:
    return GroundTruthRecord.from_ground_truth(
        label,
        case_id=incident.incident_id,
        fault_family=label.fault_family,
        difficulty=_difficulty(label),
        is_unknown=label.is_anomaly and label.root_cause_code is None,
        available_evidence_ids=(event.line_id for event in incident.events),
        metadata={
            "split": label.split,
            "group_id": label.group_id,
            "template_variant": label.template_variant,
            "root_service": label.root_service,
            "perturbations": list(label.perturbations),
            "trace_corruption_role": label.trace_corruption_role,
        },
    )


def _diagnose_mode(
    incidents: Sequence[Incident],
    *,
    mode: Literal["single", "multi"],
    manager: IncidentManager,
) -> tuple[list[PredictionRecord], dict[str, dict[str, str]], float]:
    """Diagnose Agent-visible incidents without accepting any truth argument."""

    predictions: list[PredictionRecord] = []
    execution_errors: dict[str, dict[str, str]] = {}
    mode_started = time.perf_counter()
    for incident in incidents:
        started = time.perf_counter()
        try:
            # Defensive copying keeps single and multi runs isolated and also
            # prevents a custom manager from changing the benchmark object.
            report = manager.diagnose(incident.model_copy(deep=True), mode=mode)
            latency_ms = (time.perf_counter() - started) * 1000.0
            predictions.append(
                PredictionRecord.from_diagnosis_report(
                    report,
                    case_id=incident.incident_id,
                    latency_ms=latency_ms,
                    metadata={
                        "mode": mode,
                        "confidence": report.confidence,
                    },
                )
            )
        except Exception as exc:  # one bad case must not erase the benchmark
            latency_ms = (time.perf_counter() - started) * 1000.0
            execution_errors[incident.incident_id] = {
                "error_type": type(exc).__name__,
                "message": str(exc)[:500],
            }
            # The failed execution is retained and scored as an abstaining
            # negative prediction; silently dropping it would inflate metrics.
            predictions.append(
                PredictionRecord(
                    case_id=incident.incident_id,
                    is_anomaly=False,
                    abstained=True,
                    latency_ms=latency_ms,
                    metadata={"mode": mode, "execution_error": type(exc).__name__},
                )
            )
    return predictions, execution_errors, (time.perf_counter() - mode_started) * 1000.0


def _failure_summary(
    truths: Sequence[GroundTruthRecord],
    predictions: Sequence[PredictionRecord],
    *,
    execution_errors: dict[str, dict[str, str]],
    limit_per_category: int,
) -> dict[str, Any]:
    truth_by_id = {item.case_id: item for item in truths}
    prediction_by_id = {item.case_id: item for item in predictions}
    categories: dict[str, list[dict[str, Any]]] = {
        "misclassification": [],
        "wrong_root_cause": [],
        "incomplete_evidence": [],
        "execution_error": [],
    }

    for case_id, truth in truth_by_id.items():
        prediction = prediction_by_id[case_id]
        common = {
            "case_id": case_id,
            "fault_family": truth.fault_family,
            "difficulty": truth.difficulty,
            "perturbations": list(truth.metadata.get("perturbations", [])),
        }
        if truth.is_anomaly != prediction.is_anomaly:
            categories["misclassification"].append(
                {
                    **common,
                    "expected_anomaly": truth.is_anomaly,
                    "predicted_anomaly": prediction.is_anomaly,
                }
            )

        predicted_root = (
            prediction.ranked_root_causes[0]
            if prediction.is_anomaly and prediction.ranked_root_causes
            else None
        )
        root_is_wrong = False
        if truth.is_anomaly and truth.is_unknown:
            root_is_wrong = not prediction.did_abstain
        elif truth.is_anomaly and truth.root_cause_code:
            root_is_wrong = predicted_root != truth.root_cause_code
        if root_is_wrong:
            categories["wrong_root_cause"].append(
                {
                    **common,
                    "expected_root_cause": truth.root_cause_code,
                    "predicted_root_cause": predicted_root,
                    "ranked_root_causes": list(prediction.ranked_root_causes[:3]),
                    "expected_abstention": truth.is_unknown,
                    "predicted_abstention": prediction.did_abstain,
                }
            )

        if truth.evidence_ids is not None:
            missing = sorted(set(truth.evidence_ids) - set(prediction.evidence_ids))
            if missing:
                categories["incomplete_evidence"].append(
                    {
                        **common,
                        "missing_evidence_ids": missing,
                        "expected_evidence_count": len(truth.evidence_ids),
                        "predicted_evidence_count": len(prediction.evidence_ids),
                    }
                )

        if case_id in execution_errors:
            categories["execution_error"].append(
                {**common, **execution_errors[case_id]}
            )

    return {
        "counts": {name: len(items) for name, items in categories.items()},
        "examples": {
            name: items[:limit_per_category] for name, items in categories.items()
        },
        "limit_per_category": limit_per_category,
        "truncated": {
            name: len(items) > limit_per_category for name, items in categories.items()
        },
    }


def _counterfactual_safety_summary(
    truths: Sequence[GroundTruthRecord],
    predictions: Sequence[PredictionRecord],
) -> dict[str, Any]:
    """Measure behavior after the direct root-cause evidence was removed.

    Ordinary RCA accuracy necessarily marks a safe abstention as incorrect
    because the hidden historical root cause is still known.  This companion
    metric answers the different safety question: did the Agent avoid
    presenting a known root as established when its direct evidence was no
    longer present in the visible incident?
    """

    prediction_by_id = {item.case_id: item for item in predictions}
    eligible = [
        truth
        for truth in truths
        if "missing_direct" in set(truth.metadata.get("perturbations", []))
    ]
    abstained = 0
    any_root_claimed = 0
    expected_root_claimed = 0
    cases: list[dict[str, Any]] = []
    for truth in eligible:
        prediction = prediction_by_id[truth.case_id]
        primary = (
            prediction.ranked_root_causes[0]
            if prediction.ranked_root_causes
            else None
        )
        did_abstain = prediction.did_abstain
        abstained += int(did_abstain)
        any_root_claimed += int(primary is not None)
        expected_root_claimed += int(
            truth.root_cause_code is not None and primary == truth.root_cause_code
        )
        cases.append(
            {
                "case_id": truth.case_id,
                "fault_family": truth.fault_family,
                "expected_historical_root": truth.root_cause_code,
                "predicted_root": primary,
                "abstained": did_abstain,
                "confidence": prediction.metadata.get("confidence"),
            }
        )
    count = len(eligible)
    return {
        "missing_direct_cases": count,
        "abstained": abstained,
        "abstention_rate": abstained / count if count else 0.0,
        "any_known_root_claimed": any_root_claimed,
        "any_known_root_claim_rate": any_root_claimed / count if count else 0.0,
        "historical_root_claimed": expected_root_claimed,
        "historical_root_claim_rate": expected_root_claimed / count if count else 0.0,
        "cases": cases,
        "interpretation": (
            "For missing-direct counterfactuals, abstention is the conservative "
            "outcome. This metric complements rather than replaces RCA accuracy."
        ),
    }


def _visible_direct_evidence_rca_summary(
    truths: Sequence[GroundTruthRecord],
    predictions: Sequence[PredictionRecord],
) -> dict[str, Any]:
    """Report RCA separately where known-root direct evidence remains visible."""

    prediction_by_id = {item.case_id: item for item in predictions}
    eligible = [
        truth
        for truth in truths
        if truth.is_anomaly
        and not truth.is_unknown
        and truth.root_cause_code is not None
        and "missing_direct" not in set(truth.metadata.get("perturbations", []))
    ]
    correct = 0
    abstained = 0
    for truth in eligible:
        prediction = prediction_by_id[truth.case_id]
        primary = (
            prediction.ranked_root_causes[0]
            if prediction.ranked_root_causes
            else None
        )
        correct += int(primary == truth.root_cause_code)
        abstained += int(prediction.did_abstain)
    count = len(eligible)
    return {
        "eligible_cases": count,
        "top1_correct": correct,
        "top1_accuracy": correct / count if count else 0.0,
        "abstained": abstained,
        "abstention_rate": abstained / count if count else 0.0,
        "excluded_missing_direct_cases": sum(
            truth.is_anomaly
            and not truth.is_unknown
            and truth.root_cause_code is not None
            and "missing_direct" in set(truth.metadata.get("perturbations", []))
            for truth in truths
        ),
        "interpretation": (
            "Diagnostic subset with a known historical root and its direct "
            "root-cause evidence still visible. See the ordinary RCA metric for "
            "the unconditional score and counterfactual_safety for removed evidence."
        ),
    }


def _diagnose_trace_corruption_cases(
    incidents: Sequence[Incident],
    *,
    mode: Literal["single", "multi"],
    manager: IncidentManager,
) -> list[dict[str, Any]]:
    """Re-run multi-trace cases through correlation before diagnosis.

    Candidate selection uses only Agent-visible trace IDs.  The returned rows
    deliberately contain no expected label; scoring happens later after every
    correlated sub-incident has been diagnosed.
    """

    rows: list[dict[str, Any]] = []
    for original in incidents:
        trace_ids = {event.trace_id for event in original.events if event.trace_id}
        if len(trace_ids) <= 1:
            continue
        correlated = build_incidents(
            [event.model_copy(deep=True) for event in original.events]
        )
        reports = []
        errors = []
        for sub_incident in correlated:
            try:
                report = manager.diagnose(sub_incident, mode=mode)
                reports.append(report.model_dump(mode="json"))
            except Exception as exc:
                errors.append(
                    {
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                )
        original_ids = [event.line_id for event in original.events]
        regrouped_ids = [
            event.line_id
            for sub_incident in correlated
            for event in sub_incident.events
        ]
        rows.append(
            {
                "case_id": original.incident_id,
                "trace_count": len(trace_ids),
                "correlated_incident_count": len(correlated),
                "event_conserved": (
                    len(regrouped_ids) == len(original_ids)
                    and len(set(regrouped_ids)) == len(regrouped_ids)
                    and set(regrouped_ids) == set(original_ids)
                ),
                "reports": reports,
                "errors": errors,
            }
        )
    return rows


def _score_trace_correlation_resilience(
    raw_rows: Sequence[dict[str, Any]],
    truth_by_id: dict[str, GroundTruthRecord],
) -> dict[str, Any]:
    """Score correlation output without ever feeding truth into diagnosis."""

    known_cases = 0
    known_recovered = 0
    known_with_direct_cases = 0
    known_with_direct_recovered = 0
    unknown_cases = 0
    unknown_without_known_claim = 0
    normal_cases = 0
    normal_without_alert = 0
    event_conserved = 0
    errors = 0
    cases: list[dict[str, Any]] = []
    for row in raw_rows:
        truth = truth_by_id[row["case_id"]]
        reports = row["reports"]
        claimed = sorted(
            {
                str(report["root_cause_code"])
                for report in reports
                if report.get("root_cause_code")
            }
        )
        anomaly_reports = sum(bool(report.get("is_anomaly")) for report in reports)
        recovered = None
        if not truth.is_anomaly:
            normal_cases += 1
            recovered = anomaly_reports == 0
            normal_without_alert += int(recovered)
        elif truth.is_unknown:
            unknown_cases += 1
            recovered = not claimed
            unknown_without_known_claim += int(recovered)
        elif truth.root_cause_code:
            known_cases += 1
            recovered = truth.root_cause_code in claimed
            known_recovered += int(recovered)
            if "missing_direct" not in set(
                truth.metadata.get("perturbations", [])
            ):
                known_with_direct_cases += 1
                known_with_direct_recovered += int(recovered)
        event_conserved += int(bool(row["event_conserved"]))
        errors += len(row["errors"])
        cases.append(
            {
                "case_id": row["case_id"],
                "fault_family": truth.fault_family,
                "correlated_incident_count": row["correlated_incident_count"],
                "event_conserved": row["event_conserved"],
                "claimed_root_codes": claimed,
                "expected_root_code": truth.root_cause_code,
                "perturbations": list(truth.metadata.get("perturbations", [])),
                "trace_corruption_role": truth.metadata.get(
                    "trace_corruption_role"
                ),
                "recovered": recovered,
                "diagnosis_errors": row["errors"],
            }
        )
    count = len(raw_rows)
    role_counts: dict[str, int] = {}
    role_metrics: dict[str, dict[str, int | float]] = {}
    for item in cases:
        role = str(item.get("trace_corruption_role") or "unspecified")
        role_counts[role] = role_counts.get(role, 0) + 1
        bucket = role_metrics.setdefault(
            role,
            {
                "cases": 0,
                "recovered_cases": 0,
                "recovery_rate": 0.0,
                "known_root_cases": 0,
                "known_root_recovered": 0,
                "known_root_recovery_rate": 0.0,
            },
        )
        bucket["cases"] = int(bucket["cases"]) + 1
        bucket["recovered_cases"] = int(bucket["recovered_cases"]) + int(
            item["recovered"] is True
        )
        if item.get("expected_root_code") is not None:
            bucket["known_root_cases"] = int(bucket["known_root_cases"]) + 1
            bucket["known_root_recovered"] = int(
                bucket["known_root_recovered"]
            ) + int(item["recovered"] is True)
    for bucket in role_metrics.values():
        bucket["recovery_rate"] = int(bucket["recovered_cases"]) / max(
            int(bucket["cases"]), 1
        )
        bucket["known_root_recovery_rate"] = int(
            bucket["known_root_recovered"]
        ) / max(int(bucket["known_root_cases"]), 1)
    return {
        "eligible_trace_corruption_cases": count,
        "event_conservation_rate": event_conserved / count if count else 0.0,
        "known_root_cases": known_cases,
        "known_root_recovered": known_recovered,
        "known_root_recovery_rate": (
            known_recovered / known_cases if known_cases else 0.0
        ),
        "known_root_with_direct_evidence_cases": known_with_direct_cases,
        "known_root_with_direct_evidence_recovered": known_with_direct_recovered,
        "known_root_with_direct_evidence_recovery_rate": (
            known_with_direct_recovered / known_with_direct_cases
            if known_with_direct_cases
            else 0.0
        ),
        "unknown_cases": unknown_cases,
        "unknown_without_known_claim_rate": (
            unknown_without_known_claim / unknown_cases if unknown_cases else 0.0
        ),
        "normal_cases": normal_cases,
        "normal_without_alert_rate": (
            normal_without_alert / normal_cases if normal_cases else 0.0
        ),
        "diagnosis_error_count": errors,
        "trace_corruption_role_counts": dict(sorted(role_counts.items())),
        "trace_corruption_role_metrics": dict(sorted(role_metrics.items())),
        "cases": cases,
        "interpretation": (
            "Events were re-correlated by trace ID and every resulting incident "
            "was diagnosed; recovery succeeds when any correlated incident finds "
            "the hidden historical root. This suite is separate from base latency."
        ),
    }


def _compact_ablation(comparison: dict[str, Any]) -> dict[str, Any]:
    """Avoid duplicating per-case rows already present in each mode result."""

    return {
        "baseline": comparison["baseline"],
        "variant_metrics": {
            name: payload["metrics"]
            for name, payload in comparison["variants"].items()
        },
        "deltas_vs_baseline": comparison["deltas_vs_baseline"],
        "delta_semantics": comparison["delta_semantics"],
    }


def run_p0_benchmark(
    *,
    background_events: Sequence[LogEvent] | None = None,
    case_count: int = DEFAULT_CASE_COUNT,
    seed: int = DEFAULT_SEED,
    split: BenchmarkSplit = "all",
    mode: BenchmarkMode = "both",
    failure_limit: int = 20,
    bootstrap_iterations: int = 1000,
    export_dir: str | Path | None = None,
    manager: IncidentManager | None = None,
) -> dict[str, Any]:
    """Run the complete offline label-separated benchmark and optionally export artifacts.

    The dataset generator enforces ``case_count >= 100``.  ``split`` controls
    which incidents are diagnosed, while export always writes the complete
    corpus/truth/manifest trio so the manifest hash remains independently
    verifiable.
    """

    if split not in {"all", "train", "dev", "test"}:
        raise ValueError("split must be one of: all, train, dev, test")
    if mode not in {"single", "multi", "both"}:
        raise ValueError("mode must be one of: single, multi, both")
    if failure_limit < 0:
        raise ValueError("failure_limit must be non-negative")
    if bootstrap_iterations < 0:
        raise ValueError("bootstrap_iterations must be non-negative")

    artifacts = build_blind_dataset(
        case_count=case_count,
        seed=seed,
        background_messages=background_events,
    )
    if not verify_manifest(artifacts):
        raise RuntimeError("generated blind dataset failed manifest verification")
    incidents = _selected_incidents(artifacts, split)
    diagnostic_manager = manager or IncidentManager()
    modes: tuple[Literal["single", "multi"], ...] = (
        ("single", "multi") if mode == "both" else (mode,)  # type: ignore[assignment]
    )

    predictions_by_mode: dict[str, list[PredictionRecord]] = {}
    errors_by_mode: dict[str, dict[str, dict[str, str]]] = {}
    wall_time_by_mode: dict[str, float] = {}
    # Important leakage boundary: all calls above this comment use only the
    # Agent-visible corpus.  Sidecar labels are joined only after diagnosis.
    for active_mode in modes:
        predictions, errors, wall_time_ms = _diagnose_mode(
            incidents,
            mode=active_mode,
            manager=diagnostic_manager,
        )
        predictions_by_mode[active_mode] = predictions
        errors_by_mode[active_mode] = errors
        wall_time_by_mode[active_mode] = wall_time_ms

    truths = [
        _truth_record(incident, artifacts.truth_sidecar.labels[incident.incident_id])
        for incident in incidents
    ]
    truth_by_id = {truth.case_id: truth for truth in truths}
    mode_results: dict[str, Any] = {}
    for active_mode in modes:
        trace_rows = _diagnose_trace_corruption_cases(
            incidents,
            mode=active_mode,
            manager=diagnostic_manager,
        )
        evaluation = evaluate_records(
            truths,
            predictions_by_mode[active_mode],
            bootstrap_iterations=bootstrap_iterations,
            seed=seed,
        )
        mode_results[active_mode] = {
            "evaluation": evaluation.to_dict(),
            "visible_direct_evidence_rca": _visible_direct_evidence_rca_summary(
                truths, predictions_by_mode[active_mode]
            ),
            "counterfactual_safety": _counterfactual_safety_summary(
                truths, predictions_by_mode[active_mode]
            ),
            "correlation_resilience": _score_trace_correlation_resilience(
                trace_rows, truth_by_id
            ),
            "case_failures": _failure_summary(
                truths,
                predictions_by_mode[active_mode],
                execution_errors=errors_by_mode[active_mode],
                limit_per_category=failure_limit,
            ),
            "wall_time_ms": round(wall_time_by_mode[active_mode], 3),
        }

    ablation: dict[str, Any] | None = None
    if mode == "both":
        comparison = compare_ablations(
            truths,
            predictions_by_mode,
            baseline="single",
            # Full per-mode CIs are already calculated above.  The ablation
            # table needs point deltas and should not repeat bootstrap work.
            bootstrap_iterations=0,
            seed=seed,
        )
        ablation = _compact_ablation(comparison)

    result: dict[str, Any] = {
        "benchmark": "offline-blind-log-rca-p0",
        "dataset_manifest": artifacts.manifest.model_dump(mode="json"),
        "run_config": {
            "split": split,
            "mode": mode,
            "selected_cases": len(incidents),
            "case_count": case_count,
            "seed": seed,
            "failure_limit": failure_limit,
            "bootstrap_iterations": bootstrap_iterations,
            "manager_received_truth": False,
        },
        "modes": mode_results,
        "single_vs_multi_ablation": ablation,
        "exported_files": {},
    }

    if export_dir is not None:
        paths = export_blind_dataset(artifacts, export_dir)
        result_path = Path(export_dir) / "result.json"
        exported = {name: str(path.resolve()) for name, path in paths.items()}
        exported["result"] = str(result_path.resolve())
        result["exported_files"] = exported
        result_path.write_text(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )

    return result


__all__ = ["BenchmarkMode", "BenchmarkSplit", "run_p0_benchmark"]
