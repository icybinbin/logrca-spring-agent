"""Reusable, leakage-resistant evaluation contracts for log RCA agents.

The original :mod:`logrca.evaluation` module is intentionally a tiny smoke
benchmark for the six built-in scenarios.  This module is the more general
evaluation layer used by blind, held-out and ablation experiments.

Two rules are deliberately encoded in the API:

* ``GroundTruthRecord`` and ``PredictionRecord`` are constructed separately.
  The diagnosis code never needs access to the ground-truth object.
* ``evidence_ids=None`` means "not annotated" and is excluded from evidence
  metrics.  An empty collection means "annotated and no evidence is expected".

No third-party statistics package is required.  Confidence intervals use a
deterministic case-level non-parametric bootstrap, which preserves the
relationships between detection, RCA and evidence predictions for each case.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar


def _normalise_ids(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    # Preserve rank/order while making duplicated citations harmless.
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _normalise_optional_non_negative(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return int(value)


@dataclass(slots=True)
class GroundTruthRecord:
    """Hidden labels for one incident.

    ``is_unknown`` marks an anomalous incident whose root-cause class is not in
    the supported catalogue.  A normal incident is *not* an unknown fault.
    ``available_evidence_ids`` is the complete set of IDs the agent was allowed
    to cite; when supplied it enables invalid-citation measurement.
    """

    case_id: str
    is_anomaly: bool
    root_cause_code: str | None = None
    evidence_ids: tuple[str, ...] | None = None
    is_unknown: bool = False
    fault_family: str = "unspecified"
    difficulty: str = "unspecified"
    available_evidence_ids: tuple[str, ...] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.case_id = str(self.case_id)
        if not self.case_id:
            raise ValueError("case_id must not be empty")
        self.root_cause_code = (
            str(self.root_cause_code) if self.root_cause_code is not None else None
        )
        self.evidence_ids = _normalise_ids(self.evidence_ids)
        self.available_evidence_ids = _normalise_ids(self.available_evidence_ids)
        self.fault_family = str(self.fault_family or "unspecified")
        self.difficulty = str(self.difficulty or "unspecified")
        if self.is_unknown and not self.is_anomaly:
            raise ValueError("only anomalous cases can be marked is_unknown")

    @classmethod
    def from_ground_truth(
        cls,
        truth: Any,
        *,
        case_id: str | None = None,
        fault_family: str = "unspecified",
        difficulty: str = "unspecified",
        is_unknown: bool | None = None,
        available_evidence_ids: Iterable[str] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "GroundTruthRecord":
        """Adapt the demo's ``GroundTruth`` (or a compatible object)."""

        anomaly = bool(getattr(truth, "is_anomaly"))
        root = getattr(truth, "root_cause_code", None)
        if is_unknown is None:
            is_unknown = anomaly and root is None
        return cls(
            case_id=case_id or str(getattr(truth, "scenario")),
            is_anomaly=anomaly,
            root_cause_code=root,
            evidence_ids=getattr(truth, "evidence_ids", None),
            is_unknown=is_unknown,
            fault_family=fault_family,
            difficulty=difficulty,
            available_evidence_ids=available_evidence_ids,
            metadata=dict(metadata or {}),
        )


@dataclass(slots=True)
class PredictionRecord:
    """Normalised output and runtime telemetry for one diagnosis.

    The ranked root-cause list starts with the primary answer and may contain
    alternatives.  ``abstained=None`` lets the evaluator infer abstention from
    an empty ranking; set it explicitly when a model has a dedicated abstain
    action.  Optional claim/citation fields are only scored when their required
    denominators are available.
    """

    case_id: str
    is_anomaly: bool
    ranked_root_causes: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    abstained: bool | None = None
    citation_ids: tuple[str, ...] | None = None
    invalid_citation_count: int | None = None
    unsupported_claim_count: int | None = None
    claim_count: int | None = None
    tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.case_id = str(self.case_id)
        if not self.case_id:
            raise ValueError("case_id must not be empty")
        self.ranked_root_causes = _normalise_ids(self.ranked_root_causes) or ()
        self.evidence_ids = _normalise_ids(self.evidence_ids) or ()
        self.citation_ids = _normalise_ids(self.citation_ids)
        self.invalid_citation_count = _normalise_optional_non_negative(
            self.invalid_citation_count, "invalid_citation_count"
        )
        self.unsupported_claim_count = _normalise_optional_non_negative(
            self.unsupported_claim_count, "unsupported_claim_count"
        )
        self.claim_count = _normalise_optional_non_negative(
            self.claim_count, "claim_count"
        )
        self.tool_calls = _normalise_optional_non_negative(self.tool_calls, "tool_calls")
        self.input_tokens = _normalise_optional_non_negative(
            self.input_tokens, "input_tokens"
        )
        self.output_tokens = _normalise_optional_non_negative(
            self.output_tokens, "output_tokens"
        )
        if self.claim_count is not None and self.unsupported_claim_count is not None:
            if self.unsupported_claim_count > self.claim_count:
                raise ValueError("unsupported_claim_count cannot exceed claim_count")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")

    @property
    def did_abstain(self) -> bool:
        return self.abstained if self.abstained is not None else not self.ranked_root_causes

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @classmethod
    def from_diagnosis_report(
        cls,
        report: Any,
        *,
        case_id: str | None = None,
        citation_ids: Iterable[str] | None = None,
        invalid_citation_count: int | None = None,
        unsupported_claim_count: int | None = None,
        claim_count: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        latency_ms: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "PredictionRecord":
        """Adapt a ``DiagnosisReport`` without coupling to Pydantic internals."""

        ranking: list[str] = []
        primary = getattr(report, "root_cause_code", None)
        if primary:
            ranking.append(str(primary))
        for hypothesis in getattr(report, "alternative_hypotheses", ()):
            code = getattr(hypothesis, "code", None)
            if code and str(code) not in ranking:
                ranking.append(str(code))
        evidence = tuple(
            str(item.line_id)
            for item in getattr(report, "evidence", ())
            if getattr(item, "line_id", None)
        )
        return cls(
            case_id=case_id or str(getattr(report, "incident_id")),
            is_anomaly=bool(getattr(report, "is_anomaly")),
            ranked_root_causes=tuple(ranking),
            evidence_ids=evidence,
            citation_ids=citation_ids,
            invalid_citation_count=invalid_citation_count,
            unsupported_claim_count=unsupported_claim_count,
            claim_count=claim_count,
            tool_calls=getattr(report, "tool_calls", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            metadata=dict(metadata or {}),
        )


@dataclass(slots=True)
class EvaluationResult:
    """Serializable result of a benchmark run."""

    metrics: dict[str, Any]
    confidence_intervals: dict[str, dict[str, float | int]]
    strata: dict[str, dict[str, dict[str, Any]]]
    cases: list[dict[str, Any]]
    config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": self.metrics,
            "confidence_intervals": self.confidence_intervals,
            "strata": self.strata,
            "cases": self.cases,
            "config": self.config,
        }

    def to_json(self, *, indent: int = 2, ensure_ascii: bool = False) -> str:
        return json.dumps(
            self.to_dict(),
            indent=indent,
            ensure_ascii=ensure_ascii,
            sort_keys=True,
            allow_nan=False,
        )


Pair = tuple[GroundTruthRecord, PredictionRecord]


def _safe_div(numerator: float, denominator: float, *, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def _prf(tp: int, fp: int, fn: int, *, empty_is_perfect: bool = False) -> tuple[float, float, float]:
    if empty_is_perfect and tp == fp == fn == 0:
        return 1.0, 1.0, 1.0
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return precision, recall, f1


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot compute a percentile of an empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def _mean_optional(values: Iterable[float | int | None]) -> tuple[float | None, int]:
    present = [float(value) for value in values if value is not None]
    return ((_safe_div(sum(present), len(present)) if present else None), len(present))


def _compute_metrics(pairs: Sequence[Pair]) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    for truth, prediction in pairs:
        if truth.is_anomaly and prediction.is_anomaly:
            tp += 1
        elif not truth.is_anomaly and prediction.is_anomaly:
            fp += 1
        elif truth.is_anomaly and not prediction.is_anomaly:
            fn += 1
        else:
            tn += 1
    detection_precision, detection_recall, detection_f1 = _prf(tp, fp, fn)

    rca_pairs = [
        (truth, prediction)
        for truth, prediction in pairs
        if truth.is_anomaly and not truth.is_unknown and truth.root_cause_code
    ]
    reciprocal_ranks: list[float] = []
    top1_correct = top3_correct = 0
    answered_known = 0
    for truth, prediction in rca_pairs:
        ranking = prediction.ranked_root_causes if prediction.is_anomaly else ()
        answered_known += bool(ranking and not prediction.did_abstain)
        try:
            rank = ranking.index(str(truth.root_cause_code)) + 1
        except ValueError:
            rank = 0
        top1_correct += rank == 1
        top3_correct += 0 < rank <= 3
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

    unknown_pairs = [
        (truth, prediction)
        for truth, prediction in pairs
        if truth.is_anomaly and truth.is_unknown
    ]
    unknown_abstained = sum(prediction.did_abstain for _, prediction in unknown_pairs)

    evidence_pairs = [
        (truth, prediction) for truth, prediction in pairs if truth.evidence_ids is not None
    ]
    evidence_tp = evidence_fp = evidence_fn = 0
    macro_precision: list[float] = []
    macro_recall: list[float] = []
    macro_f1: list[float] = []
    exact_matches = 0
    for truth, prediction in evidence_pairs:
        expected = set(truth.evidence_ids or ())
        predicted = set(prediction.evidence_ids)
        intersection = len(expected & predicted)
        case_tp = intersection
        case_fp = len(predicted - expected)
        case_fn = len(expected - predicted)
        evidence_tp += case_tp
        evidence_fp += case_fp
        evidence_fn += case_fn
        case_precision, case_recall, case_f1 = _prf(
            case_tp, case_fp, case_fn, empty_is_perfect=True
        )
        macro_precision.append(case_precision)
        macro_recall.append(case_recall)
        macro_f1.append(case_f1)
        exact_matches += expected == predicted
    micro_precision, micro_recall, micro_f1 = _prf(
        evidence_tp, evidence_fp, evidence_fn, empty_is_perfect=True
    )

    citation_cases = 0
    invalid_citations = total_citations = 0
    for truth, prediction in pairs:
        citations = prediction.citation_ids
        if citations is None:
            citations = prediction.evidence_ids
        explicit = prediction.invalid_citation_count
        if explicit is not None:
            invalid = explicit
        elif truth.available_evidence_ids is not None:
            invalid = len(set(citations) - set(truth.available_evidence_ids))
        else:
            continue
        citation_cases += 1
        invalid_citations += invalid
        # Explicit validators may count malformed citations not represented by an ID.
        total_citations += max(len(citations), invalid)

    claim_cases = unsupported_claims = total_claims = 0
    for _, prediction in pairs:
        if prediction.unsupported_claim_count is None or prediction.claim_count is None:
            continue
        claim_cases += 1
        unsupported_claims += prediction.unsupported_claim_count
        total_claims += prediction.claim_count

    tool_mean, tool_count = _mean_optional(prediction.tool_calls for _, prediction in pairs)
    input_mean, input_count = _mean_optional(prediction.input_tokens for _, prediction in pairs)
    output_mean, output_count = _mean_optional(prediction.output_tokens for _, prediction in pairs)
    token_mean, token_count = _mean_optional(prediction.total_tokens for _, prediction in pairs)
    cost_mean, cost_count = _mean_optional(prediction.cost_usd for _, prediction in pairs)
    latency_values = [
        float(prediction.latency_ms)
        for _, prediction in pairs
        if prediction.latency_ms is not None
    ]

    return {
        "cases": len(pairs),
        "detection": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": detection_precision,
            "recall": detection_recall,
            "f1": detection_f1,
        },
        "rca": {
            "eligible_cases": len(rca_pairs),
            "top1_accuracy": _safe_div(top1_correct, len(rca_pairs)),
            "top3_accuracy": _safe_div(top3_correct, len(rca_pairs)),
            "mrr": _safe_div(sum(reciprocal_ranks), len(reciprocal_ranks)),
            "known_answer_rate": _safe_div(answered_known, len(rca_pairs)),
        },
        "unknown": {
            "eligible_cases": len(unknown_pairs),
            "abstained": unknown_abstained,
            "abstention_rate": _safe_div(unknown_abstained, len(unknown_pairs)),
        },
        "evidence": {
            "eligible_cases": len(evidence_pairs),
            "tp": evidence_tp,
            "fp": evidence_fp,
            "fn": evidence_fn,
            "micro_precision": micro_precision,
            "micro_recall": micro_recall,
            "micro_f1": micro_f1,
            "macro_precision": _safe_div(sum(macro_precision), len(macro_precision)),
            "macro_recall": _safe_div(sum(macro_recall), len(macro_recall)),
            "macro_f1": _safe_div(sum(macro_f1), len(macro_f1)),
            "exact_set_accuracy": _safe_div(exact_matches, len(evidence_pairs)),
        },
        "grounding": {
            "citation_cases_assessed": citation_cases,
            "invalid_citations": invalid_citations,
            "total_citations": total_citations,
            "invalid_citation_rate": (
                _safe_div(invalid_citations, total_citations)
                if citation_cases
                else None
            ),
            "claim_cases_assessed": claim_cases,
            "unsupported_claims": unsupported_claims,
            "total_claims": total_claims,
            "unsupported_claim_rate": (
                _safe_div(unsupported_claims, total_claims) if claim_cases else None
            ),
        },
        "efficiency": {
            "average_tool_calls": tool_mean,
            "tool_call_cases": tool_count,
            "total_tool_calls": sum(
                prediction.tool_calls or 0 for _, prediction in pairs
            ),
            "average_input_tokens": input_mean,
            "input_token_cases": input_count,
            "total_input_tokens": sum(
                prediction.input_tokens or 0 for _, prediction in pairs
            ),
            "average_output_tokens": output_mean,
            "output_token_cases": output_count,
            "total_output_tokens": sum(
                prediction.output_tokens or 0 for _, prediction in pairs
            ),
            "average_total_tokens": token_mean,
            "token_cases": token_count,
            "total_tokens": sum(prediction.total_tokens or 0 for _, prediction in pairs),
            "average_cost_usd": cost_mean,
            "cost_cases": cost_count,
            "total_cost_usd": (
                sum(prediction.cost_usd or 0.0 for _, prediction in pairs)
                if cost_count
                else None
            ),
            "mean_latency_ms": (
                _safe_div(sum(latency_values), len(latency_values))
                if latency_values
                else None
            ),
            "p50_latency_ms": _percentile(latency_values, 0.50) if latency_values else None,
            "p95_latency_ms": _percentile(latency_values, 0.95) if latency_values else None,
            "latency_cases": len(latency_values),
        },
    }


def _metric_at(metrics: Mapping[str, Any], path: str) -> float | None:
    value: Any = metrics
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


_BOOTSTRAP_PATHS = (
    "detection.precision",
    "detection.recall",
    "detection.f1",
    "rca.top1_accuracy",
    "rca.top3_accuracy",
    "rca.mrr",
    "unknown.abstention_rate",
    "evidence.micro_precision",
    "evidence.micro_recall",
    "evidence.micro_f1",
    "evidence.macro_precision",
    "evidence.macro_recall",
    "evidence.macro_f1",
    "evidence.exact_set_accuracy",
    "grounding.invalid_citation_rate",
    "grounding.unsupported_claim_rate",
    "efficiency.average_tool_calls",
    "efficiency.average_total_tokens",
    "efficiency.average_cost_usd",
    "efficiency.mean_latency_ms",
    "efficiency.p95_latency_ms",
)


def _bootstrap_metric_is_defined(metrics: Mapping[str, Any], path: str) -> bool:
    """Exclude resamples where a conditional metric has no eligible cases.

    For example, a bootstrap draw from a small benchmark can contain no unknown
    faults.  Treating its unknown-abstention rate as zero would bias the interval
    downward; that draw is undefined for this metric and is therefore skipped.
    """

    if path == "detection.precision":
        section = metrics["detection"]
        return section["tp"] + section["fp"] > 0
    if path == "detection.recall":
        section = metrics["detection"]
        return section["tp"] + section["fn"] > 0
    if path == "detection.f1":
        section = metrics["detection"]
        return 2 * section["tp"] + section["fp"] + section["fn"] > 0
    if path.startswith("rca."):
        return metrics["rca"]["eligible_cases"] > 0
    if path.startswith("unknown."):
        return metrics["unknown"]["eligible_cases"] > 0
    if path.startswith("evidence."):
        return metrics["evidence"]["eligible_cases"] > 0
    if path == "grounding.invalid_citation_rate":
        return metrics["grounding"]["citation_cases_assessed"] > 0
    if path == "grounding.unsupported_claim_rate":
        return metrics["grounding"]["claim_cases_assessed"] > 0
    if path == "efficiency.average_tool_calls":
        return metrics["efficiency"]["tool_call_cases"] > 0
    if path == "efficiency.average_total_tokens":
        return metrics["efficiency"]["token_cases"] > 0
    if path == "efficiency.average_cost_usd":
        return metrics["efficiency"]["cost_cases"] > 0
    if path in {"efficiency.mean_latency_ms", "efficiency.p95_latency_ms"}:
        return metrics["efficiency"]["latency_cases"] > 0
    return True


def _bootstrap_confidence_intervals(
    pairs: Sequence[Pair],
    point_metrics: Mapping[str, Any],
    *,
    iterations: int,
    confidence_level: float,
    seed: int,
) -> dict[str, dict[str, float | int]]:
    if iterations <= 0 or not pairs:
        return {}
    if not 0.0 < confidence_level < 1.0:
        raise ValueError("confidence_level must be between 0 and 1")
    generator = random.Random(seed)
    distributions: dict[str, list[float]] = defaultdict(list)
    size = len(pairs)
    for _ in range(iterations):
        sampled = [pairs[generator.randrange(size)] for _ in range(size)]
        sample_metrics = _compute_metrics(sampled)
        for path in _BOOTSTRAP_PATHS:
            if not _bootstrap_metric_is_defined(sample_metrics, path):
                continue
            value = _metric_at(sample_metrics, path)
            if value is not None:
                distributions[path].append(value)

    alpha = (1.0 - confidence_level) / 2.0
    result: dict[str, dict[str, float | int]] = {}
    for path, values in distributions.items():
        if not _bootstrap_metric_is_defined(point_metrics, path):
            continue
        estimate = _metric_at(point_metrics, path)
        if estimate is None or not values:
            continue
        result[path] = {
            "estimate": estimate,
            "lower": _percentile(values, alpha),
            "upper": _percentile(values, 1.0 - alpha),
            "confidence_level": confidence_level,
            "iterations": iterations,
            "valid_samples": len(values),
        }
    return result


def _case_row(truth: GroundTruthRecord, prediction: PredictionRecord) -> dict[str, Any]:
    expected = set(truth.evidence_ids or ()) if truth.evidence_ids is not None else None
    predicted = set(prediction.evidence_ids)
    root_rank: int | None = None
    if truth.root_cause_code:
        try:
            root_rank = prediction.ranked_root_causes.index(truth.root_cause_code) + 1
        except ValueError:
            root_rank = None
    return {
        "case_id": truth.case_id,
        "fault_family": truth.fault_family,
        "difficulty": truth.difficulty,
        "expected_anomaly": truth.is_anomaly,
        "predicted_anomaly": prediction.is_anomaly,
        "is_unknown": truth.is_unknown,
        "abstained": prediction.did_abstain,
        "expected_root_cause": truth.root_cause_code,
        "ranked_root_causes": list(prediction.ranked_root_causes),
        "root_cause_rank": root_rank,
        "expected_evidence": sorted(expected) if expected is not None else None,
        "predicted_evidence": sorted(predicted),
        "evidence_exact_match": expected == predicted if expected is not None else None,
        "tool_calls": prediction.tool_calls,
        "input_tokens": prediction.input_tokens,
        "output_tokens": prediction.output_tokens,
        "cost_usd": prediction.cost_usd,
        "latency_ms": prediction.latency_ms,
    }


def _index_unique(records: Iterable[Any], record_name: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for record in records:
        case_id = str(record.case_id)
        if case_id in indexed:
            raise ValueError(f"duplicate {record_name} case_id: {case_id}")
        indexed[case_id] = record
    return indexed


def evaluate_records(
    truths: Iterable[GroundTruthRecord],
    predictions: Iterable[PredictionRecord],
    *,
    bootstrap_iterations: int = 1000,
    confidence_level: float = 0.95,
    seed: int = 20260921,
) -> EvaluationResult:
    """Evaluate aligned prediction and ground-truth records.

    Every truth must have exactly one prediction and vice versa.  Failing fast
    prevents silently optimistic scores caused by dropped hard examples.
    """

    if bootstrap_iterations < 0:
        raise ValueError("bootstrap_iterations must be non-negative")
    truth_index = _index_unique(truths, "ground-truth")
    prediction_index = _index_unique(predictions, "prediction")
    missing = sorted(set(truth_index) - set(prediction_index))
    unexpected = sorted(set(prediction_index) - set(truth_index))
    if missing or unexpected:
        raise ValueError(
            f"case_id mismatch; missing_predictions={missing}, "
            f"unexpected_predictions={unexpected}"
        )
    pairs: list[Pair] = [
        (truth, prediction_index[case_id]) for case_id, truth in truth_index.items()
    ]
    metrics = _compute_metrics(pairs)
    intervals = _bootstrap_confidence_intervals(
        pairs,
        metrics,
        iterations=bootstrap_iterations,
        confidence_level=confidence_level,
        seed=seed,
    )

    strata: dict[str, dict[str, dict[str, Any]]] = {}
    for attribute in ("fault_family", "difficulty"):
        groups: dict[str, list[Pair]] = defaultdict(list)
        for pair in pairs:
            groups[str(getattr(pair[0], attribute))].append(pair)
        strata[attribute] = {
            name: _compute_metrics(group_pairs)
            for name, group_pairs in sorted(groups.items())
        }

    return EvaluationResult(
        metrics=metrics,
        confidence_intervals=intervals,
        strata=strata,
        cases=[_case_row(truth, prediction) for truth, prediction in pairs],
        config={
            "bootstrap_iterations": bootstrap_iterations,
            "confidence_level": confidence_level,
            "seed": seed,
            "evidence_annotation_semantics": (
                "null=not annotated/excluded; empty=annotated empty set"
            ),
        },
    )


class _AblationDefaults:
    PATHS: ClassVar[tuple[str, ...]] = (
        "detection.f1",
        "rca.top1_accuracy",
        "rca.top3_accuracy",
        "rca.mrr",
        "unknown.abstention_rate",
        "evidence.micro_f1",
        "evidence.exact_set_accuracy",
        "grounding.invalid_citation_rate",
        "grounding.unsupported_claim_rate",
        "efficiency.average_tool_calls",
        "efficiency.average_total_tokens",
        "efficiency.average_cost_usd",
        "efficiency.p95_latency_ms",
    )
    LOWER_IS_BETTER: ClassVar[frozenset[str]] = frozenset(
        {
            "grounding.invalid_citation_rate",
            "grounding.unsupported_claim_rate",
            "efficiency.average_tool_calls",
            "efficiency.average_total_tokens",
            "efficiency.average_cost_usd",
            "efficiency.p95_latency_ms",
        }
    )


def compare_ablations(
    truths: Iterable[GroundTruthRecord],
    predictions_by_variant: Mapping[str, Iterable[PredictionRecord]],
    *,
    baseline: str,
    metric_paths: Iterable[str] | None = None,
    bootstrap_iterations: int = 1000,
    confidence_level: float = 0.95,
    seed: int = 20260921,
) -> dict[str, Any]:
    """Evaluate variants on the same cases and report deltas vs. ``baseline``.

    ``delta`` is always ``variant - baseline``.  ``improvement`` reverses the
    sign for cost/error metrics, so a positive value consistently means better.
    """

    if baseline not in predictions_by_variant:
        raise ValueError(f"baseline variant not found: {baseline}")
    truth_records = list(truths)
    paths = tuple(metric_paths or _AblationDefaults.PATHS)
    results = {
        name: evaluate_records(
            truth_records,
            predictions,
            bootstrap_iterations=bootstrap_iterations,
            confidence_level=confidence_level,
            seed=seed,
        )
        for name, predictions in predictions_by_variant.items()
    }
    baseline_metrics = results[baseline].metrics
    deltas: dict[str, dict[str, dict[str, float]]] = {}
    for name, result in results.items():
        if name == baseline:
            continue
        variant_deltas: dict[str, dict[str, float]] = {}
        for path in paths:
            base_value = _metric_at(baseline_metrics, path)
            value = _metric_at(result.metrics, path)
            if base_value is None or value is None:
                continue
            delta = value - base_value
            improvement = -delta if path in _AblationDefaults.LOWER_IS_BETTER else delta
            variant_deltas[path] = {
                "baseline": base_value,
                "value": value,
                "delta": delta,
                "improvement": improvement,
            }
        deltas[name] = variant_deltas
    return {
        "baseline": baseline,
        "variants": {name: result.to_dict() for name, result in results.items()},
        "deltas_vs_baseline": deltas,
        "delta_semantics": (
            "delta=value-baseline; improvement>0 always means better after "
            "reversing lower-is-better cost/error metrics"
        ),
    }


__all__ = [
    "EvaluationResult",
    "GroundTruthRecord",
    "PredictionRecord",
    "compare_ablations",
    "evaluate_records",
]
