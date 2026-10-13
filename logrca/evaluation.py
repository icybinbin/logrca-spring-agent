"""Deterministic evaluation for anomaly detection, RCA, and evidence quality."""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from .agents import IncidentManager
from .models import BenchmarkMetrics
from .synthetic import all_scenarios


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return precision, recall, f1


def run_benchmark(mode: str = "multi") -> dict[str, Any]:
    """Evaluate one mode on hidden labels from all synthetic scenarios."""

    if mode not in {"single", "multi"}:
        raise ValueError("mode must be 'single' or 'multi'")
    manager = IncidentManager()
    tp = fp = fn = 0
    root_correct = root_total = 0
    evidence_tp = evidence_predicted = evidence_expected = 0
    total_tool_calls = 0
    total_latency_ms = 0.0
    case_results: list[dict[str, Any]] = []

    for case in all_scenarios():
        started = time.perf_counter()
        report = manager.diagnose(case.incident, mode=mode)
        latency_ms = (time.perf_counter() - started) * 1000
        total_latency_ms += latency_ms
        total_tool_calls += report.tool_calls

        if report.is_anomaly and case.truth.is_anomaly:
            tp += 1
        elif report.is_anomaly and not case.truth.is_anomaly:
            fp += 1
        elif not report.is_anomaly and case.truth.is_anomaly:
            fn += 1

        predicted_ids = {item.line_id for item in report.evidence}
        expected_ids = set(case.truth.evidence_ids)
        if case.truth.is_anomaly:
            root_total += 1
            root_correct += report.root_cause_code == case.truth.root_cause_code
            evidence_tp += len(predicted_ids & expected_ids)
            evidence_predicted += len(predicted_ids)
            evidence_expected += len(expected_ids)

        case_results.append(
            {
                "scenario": case.truth.scenario,
                "expected_anomaly": case.truth.is_anomaly,
                "predicted_anomaly": report.is_anomaly,
                "expected_root_cause": case.truth.root_cause_code,
                "predicted_root_cause": report.root_cause_code,
                "expected_evidence": sorted(expected_ids),
                "predicted_evidence": sorted(predicted_ids),
                "tool_calls": report.tool_calls,
                "latency_ms": round(latency_ms, 3),
            }
        )

    precision, recall, f1 = _prf(tp, fp, fn)
    metrics = BenchmarkMetrics(
        mode=mode,
        cases=len(case_results),
        anomaly_precision=round(precision, 4),
        anomaly_recall=round(recall, 4),
        anomaly_f1=round(f1, 4),
        root_cause_accuracy=round(_safe_div(root_correct, root_total), 4),
        evidence_precision=round(_safe_div(evidence_tp, evidence_predicted), 4),
        evidence_recall=round(_safe_div(evidence_tp, evidence_expected), 4),
        average_tool_calls=round(total_tool_calls / len(case_results), 3),
        average_latency_ms=round(total_latency_ms / len(case_results), 3),
    )
    return {
        "metrics": metrics.model_dump(mode="json"),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": len(case_results) - tp - fp - fn},
        "cases": case_results,
        "scope_note": (
            "这是确定性合成场景上的功能回归，不是生产泛化效果；"
            "离线规则与场景模板使用同一故障目录。"
        ),
    }


def compare_modes() -> dict[str, Any]:
    return {
        "single": run_benchmark("single"),
        "multi": run_benchmark("multi"),
        "interpretation": (
            "当前数据中，多 Agent 的 Evidence Auditor 以更多工具调用为代价，"
            "剔除了级联症状，使证据精确率提升；这不是多 Agent 普遍优于单 Agent 的证明。"
        ),
    }


def evaluate_bgl_level_baseline(
    path: str | Path, limit: int | None = None
) -> dict[str, Any]:
    """Line-level public-data baseline; labels never enter Agent tools."""

    tp = fp = fn = tn = 0
    rows = 0
    templates: set[str] = set()
    with Path(path).open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            if limit is not None and rows >= limit:
                break
            rows += 1
            templates.add(str(row.get("EventId", "")))
            expected = str(row.get("Label", "-")).strip() != "-"
            predicted = str(row.get("Level", "INFO")).upper() in {
                "ERROR",
                "FATAL",
                "SEVERE",
            }
            if predicted and expected:
                tp += 1
            elif predicted and not expected:
                fp += 1
            elif not predicted and expected:
                fn += 1
            else:
                tn += 1
    precision, recall, f1 = _prf(tp, fp, fn)
    return {
        "unit": "log_line",
        "rows": rows,
        "unique_templates": len(templates),
        "support_anomaly": tp + fn,
        "support_normal": fp + tn,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "baseline": "Level in {ERROR, FATAL, SEVERE}",
        "row_limit": limit,
        "scope_note": "BGL Label 是行级异常标签，不是根因标签，不能用于 RCA 评测。",
    }
