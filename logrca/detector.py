"""Cheap, explainable anomaly gate placed before expensive Agent reasoning."""

from __future__ import annotations

from .diagnostic_signatures import has_known_failure_signal
from .models import AnomalyAssessment, Incident


class AnomalyDetector:
    """A deterministic baseline that is intentionally easy to audit.

    In a production system this interface can wrap an Isolation Forest, a
    sequence model, or an alerting signal.  Keeping it outside the reasoning
    Agent is important: normal traffic should not spend LLM tokens.
    """

    def __init__(self, threshold: float = 0.35, *, admit_single_errors: bool = False) -> None:
        self.threshold = threshold
        self.admit_single_errors = admit_single_errors

    def assess(self, incident: Incident) -> AnomalyAssessment:
        levels = [event.level.upper() for event in incident.events]
        error_count = sum(
            level in {"ERROR", "CRITICAL", "FATAL", "SEVERE"} for level in levels
        )
        severe_level_count = sum(
            level in {"CRITICAL", "FATAL", "SEVERE"} for level in levels
        )
        warning_count = sum(level == "WARN" for level in levels)
        # Use observable message semantics rather than dataset EventIds.  This
        # keeps the gate stable when the P0 benchmark remaps every template ID.
        critical_count = sum(
            has_known_failure_signal(f"{event.message} {event.event_template}")
            for event in incident.events
        )
        error_services = {
            event.service
            for event in incident.events
            if event.level.upper() in {"ERROR", "CRITICAL", "FATAL", "SEVERE"}
        }

        # Multiple weak signals are capped; a known high-signal semantic match gets a
        # large boost but still remains an assessment rather than ground truth.
        score = min(
            1.0,
            error_count * 0.16
            + warning_count * 0.04
            + critical_count * 0.48
            + (0.08 if len(error_services) > 1 else 0.0),
        )
        # Known templates improve precision, but the gate must still admit new
        # severe failure types.  Otherwise the Agent's honest `unknown` branch
        # would be unreachable for exactly the incidents that need discovery.
        if severe_level_count:
            score = max(score, 0.7)
        elif error_count >= 2:
            score = max(score, 0.4)
        elif self.admit_single_errors and error_count:
            score = max(score, self.threshold)
        reasons: list[str] = []
        if error_count:
            reasons.append(f"发现 {error_count} 条 ERROR/CRITICAL 日志")
        if warning_count:
            reasons.append(f"发现 {warning_count} 条 WARN 日志")
        if critical_count:
            reasons.append(f"命中 {critical_count} 个高风险故障语义信号")
        if severe_level_count:
            reasons.append(f"发现 {severe_level_count} 条 CRITICAL/FATAL，强制进入诊断")
        elif self.admit_single_errors and error_count == 1:
            reasons.append("当前门控配置允许单条 ERROR 进入排查；是否能确认根因由后续证据决定")
        if len(error_services) > 1:
            reasons.append(f"错误跨越 {len(error_services)} 个服务")
        if not reasons:
            reasons.append("未发现错误级别或高风险模板")

        return AnomalyAssessment(
            is_anomaly=score >= self.threshold,
            score=round(score, 4),
            reasons=reasons,
            error_count=error_count,
            warning_count=warning_count,
            critical_template_count=critical_count,
        )
