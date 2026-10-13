"""Shared data contracts for the demo.

Why this file matters for Agent engineering:

* Agents should not pass unstructured prose to one another when a stable
  schema is possible.  Typed contracts make orchestration testable.
* Evidence is represented by log IDs, not copied quotations.  That lets the
  final answer be checked against the source and reduces prompt/token size.
* The report separates facts, hypotheses, actions, and limitations so a model
  cannot silently turn a guess into a fact.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


LOG_LEVEL_ALIASES = {
    "FATAL": "CRITICAL",
    "CRIT": "CRITICAL",
    "ERR": "ERROR",
    "E": "ERROR",
    "WARNING": "WARN",
    "W": "WARN",
    "I": "INFO",
    "D": "DEBUG",
    "V": "TRACE",
}


def normalize_log_level(value: object) -> str:
    """Normalize common long and Android one-letter level spellings."""

    upper = str(value or "INFO").strip().upper() or "INFO"
    return LOG_LEVEL_ALIASES.get(upper, upper)


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEvent(BaseModel):
    """One normalized log event, independent of its original log format."""

    line_id: str
    timestamp: datetime
    service: str
    level: str = "INFO"
    message: str
    template_id: str
    event_template: str
    trace_id: str | None = None
    source: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("level")
    @classmethod
    def normalize_level(cls, value: str) -> str:
        return normalize_log_level(value)


class Incident(BaseModel):
    """A bounded group of events that should be diagnosed together."""

    incident_id: str
    title: str
    events: list[LogEvent]
    source: str = "synthetic"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def start_time(self) -> datetime | None:
        return min((event.timestamp for event in self.events), default=None)

    @property
    def end_time(self) -> datetime | None:
        return max((event.timestamp for event in self.events), default=None)


class GroundTruth(BaseModel):
    """Labels used only by evaluation, never exposed to the diagnosis policy."""

    scenario: str
    is_anomaly: bool
    root_cause_code: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SyntheticCase(BaseModel):
    incident: Incident
    truth: GroundTruth


EvidenceKind = Literal["direct", "support", "symptom", "contradiction"]


class EvidenceRef(BaseModel):
    line_id: str
    kind: EvidenceKind
    summary: str
    weight: float = Field(ge=0.0, le=1.0)


class Hypothesis(BaseModel):
    code: str
    title: str
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str
    evidence: list[EvidenceRef] = Field(default_factory=list)


class HypothesisBundle(BaseModel):
    incident_id: str
    hypotheses: list[Hypothesis]
    searched_event_ids: list[str] = Field(default_factory=list)
    runbook_checks: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    tool_calls: int = 0


class AuditResult(BaseModel):
    accepted: list[EvidenceRef]
    rejected: list[EvidenceRef]
    confidence_multiplier: float = Field(default=1.0, ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


class AnomalyAssessment(BaseModel):
    is_anomaly: bool
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    error_count: int
    warning_count: int
    critical_template_count: int


class DiagnosisReport(BaseModel):
    """Stable final output contract consumed by CLI/UI/evaluation."""

    incident_id: str
    mode: Literal["single", "multi"]
    is_anomaly: bool
    anomaly_score: float = Field(ge=0.0, le=1.0)
    severity: Severity
    root_cause_code: str | None = None
    root_cause: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    affected_services: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    alternative_hypotheses: list[Hypothesis] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    trace_path: str | None = None


class CompressionStats(BaseModel):
    rows: int
    unique_templates: int
    raw_characters: int
    normalized_json_characters: int
    compact_characters: int
    compact_vs_raw_ratio: float
    compact_vs_json_ratio: float


class BenchmarkMetrics(BaseModel):
    mode: Literal["single", "multi"]
    cases: int
    anomaly_precision: float
    anomaly_recall: float
    anomaly_f1: float
    root_cause_accuracy: float
    evidence_precision: float
    evidence_recall: float
    average_tool_calls: float
    average_latency_ms: float
