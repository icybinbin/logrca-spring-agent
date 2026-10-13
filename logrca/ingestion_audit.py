"""Read-only ingestion contract audit for Loghub structured CSV datasets.

The normalizer intentionally accepts heterogeneous Loghub schemas.  This
module measures how much of each schema can be normalized instead of silently
assuming every row has timestamps, levels, service names and correlation IDs.
It uses only the Python standard library and never modifies source datasets.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import normalize_log_level
from .normalizer import TIME_FALLBACK_STRATEGY, parse_time_with_strategy


_TRACE_PATTERNS = (
    re.compile(r"\b(req-[0-9a-f-]{16,})\b", re.IGNORECASE),
    re.compile(r"\b(trace[-_=][A-Za-z0-9-]+)\b", re.IGNORECASE),
    re.compile(r"\b(blk_-?\d+)\b"),
)

@dataclass
class DatasetIngestionStats:
    """Coverage and data-quality counters for one structured CSV file."""

    dataset: str
    path: str
    rows: int = 0
    time_parse_success: int = 0
    time_row_number_fallback: int = 0
    parser_strategy_distribution: dict[str, int] = field(default_factory=dict)
    level_field_present: int = 0
    level_normalization_changes: int = 0
    normalized_level_distribution: dict[str, int] = field(default_factory=dict)
    service_field_present: int = 0
    service_source_distribution: dict[str, int] = field(default_factory=dict)
    trace_id_present: int = 0
    event_id_present: int = 0
    event_template_present: int = 0
    template_or_event_id_present: int = 0
    line_id_present: int = 0
    duplicate_line_ids: int = 0
    duplicate_line_id_values: list[str] = field(default_factory=list)
    label_distribution: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def time_format_distribution(self) -> dict[str, int]:
        """Compatibility alias retained for the initial audit API."""

        return self.parser_strategy_distribution

    def coverage(self) -> dict[str, float]:
        """Return stable 0..1 coverage ratios; empty files report zero."""

        denominator = max(self.rows, 1)
        return {
            "time_parse": self.time_parse_success / denominator,
            "time_fallback": self.time_row_number_fallback / denominator,
            "level": self.level_field_present / denominator,
            "service": self.service_field_present / denominator,
            "trace_id": self.trace_id_present / denominator,
            "event_id": self.event_id_present / denominator,
            "event_template": self.event_template_present / denominator,
            "template_or_event_id": self.template_or_event_id_present / denominator,
            "line_id": self.line_id_present / denominator,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["coverage"] = self.coverage()
        return result


@dataclass
class IngestionAuditReport:
    """Repository-wide summary plus per-dataset details."""

    root: str
    datasets: list[DatasetIngestionStats]
    total_files: int
    total_rows: int
    total_time_parse_success: int
    total_time_row_number_fallback: int
    total_duplicate_line_ids: int
    total_errors: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "total_files": self.total_files,
            "total_rows": self.total_rows,
            "total_time_parse_success": self.total_time_parse_success,
            "total_time_row_number_fallback": self.total_time_row_number_fallback,
            "total_duplicate_line_ids": self.total_duplicate_line_ids,
            "total_errors": self.total_errors,
            "datasets": [dataset.to_dict() for dataset in self.datasets],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent)


def _nonempty(value: object) -> bool:
    return value is not None and bool(str(value).strip())


def _normalized_level(value: object) -> tuple[str, bool, bool]:
    raw = str(value or "").strip()
    present = bool(raw)
    upper = raw.upper() if present else "INFO"
    normalized = normalize_log_level(upper)
    return normalized, present, present and normalized != upper


def _service_field(row: dict[str | None, Any]) -> str | None:
    for field_name in ("Component", "Process", "Node"):
        if _nonempty(row.get(field_name)):
            return field_name
    return None


def _has_trace(content: str) -> bool:
    return any(pattern.search(content) for pattern in _TRACE_PATTERNS)


def _increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _row_shape_error(row: dict[str | None, Any], row_number: int) -> str | None:
    extras = row.get(None)
    if extras:
        return f"row {row_number}: {len(extras)} extra column(s) beyond header"
    return None


def audit_structured_csv(
    path: str | Path,
    *,
    limit: int | None = None,
) -> DatasetIngestionStats:
    """Audit one ``*_structured.csv`` without modifying or normalizing it."""

    csv_path = Path(path)
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")
    stats = DatasetIngestionStats(dataset=csv_path.parent.name, path=str(csv_path.resolve()))
    seen_line_ids: set[str] = set()
    duplicate_values: set[str] = set()

    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                stats.errors.append("missing CSV header")
                return stats
            for row_number, row in enumerate(reader, start=1):
                if limit is not None and stats.rows >= limit:
                    break
                stats.rows += 1
                shape_error = _row_shape_error(row, row_number)
                if shape_error:
                    stats.errors.append(shape_error)

                _, time_strategy = parse_time_with_strategy(row, row_number)
                if time_strategy != TIME_FALLBACK_STRATEGY:
                    stats.time_parse_success += 1
                else:
                    stats.time_row_number_fallback += 1
                _increment(stats.parser_strategy_distribution, time_strategy)

                normalized_level, level_present, changed = _normalized_level(row.get("Level"))
                if level_present:
                    stats.level_field_present += 1
                if changed:
                    stats.level_normalization_changes += 1
                _increment(stats.normalized_level_distribution, normalized_level)

                service_field = _service_field(row)
                if service_field:
                    stats.service_field_present += 1
                    _increment(stats.service_source_distribution, service_field)

                content = f"{row.get('Content') or row.get('Message') or ''} {row.get('ADDR') or ''}"
                if _has_trace(content):
                    stats.trace_id_present += 1

                has_event_id = _nonempty(row.get("EventId"))
                has_template = _nonempty(row.get("EventTemplate"))
                stats.event_id_present += int(has_event_id)
                stats.event_template_present += int(has_template)
                stats.template_or_event_id_present += int(has_event_id or has_template)

                raw_line_id = str(row.get("LineId") or "").strip()
                if raw_line_id:
                    stats.line_id_present += 1
                    if raw_line_id in seen_line_ids:
                        stats.duplicate_line_ids += 1
                        duplicate_values.add(raw_line_id)
                    else:
                        seen_line_ids.add(raw_line_id)

                label = str(row.get("Label") or "").strip() or "<missing>"
                _increment(stats.label_distribution, label)
    except (OSError, UnicodeError, csv.Error) as exc:
        stats.errors.append(f"{type(exc).__name__}: {exc}")

    stats.duplicate_line_id_values = sorted(duplicate_values)
    stats.parser_strategy_distribution = dict(
        sorted(stats.parser_strategy_distribution.items())
    )
    stats.normalized_level_distribution = dict(sorted(stats.normalized_level_distribution.items()))
    stats.service_source_distribution = dict(sorted(stats.service_source_distribution.items()))
    stats.label_distribution = dict(sorted(stats.label_distribution.items()))
    return stats


def discover_structured_csv(root: str | Path) -> list[Path]:
    """Find Loghub ``<dataset>/*_structured.csv`` files deterministically."""

    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Loghub root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Loghub root is not a directory: {root_path}")
    return sorted(
        (path for path in root_path.glob("*/*_structured.csv") if path.is_file()),
        key=lambda item: (item.parent.name.lower(), item.name.lower()),
    )


def audit_loghub_root(
    root: str | Path,
    *,
    limit: int | None = None,
) -> IngestionAuditReport:
    """Audit every discovered dataset and aggregate repository totals.

    ``limit`` applies independently to each CSV.  This makes a quick 2k-style
    coverage pass comparable across datasets while preserving deterministic
    totals.
    """

    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative or None")
    root_path = Path(root).resolve()
    datasets = [audit_structured_csv(path, limit=limit) for path in discover_structured_csv(root_path)]
    return IngestionAuditReport(
        root=str(root_path),
        datasets=datasets,
        total_files=len(datasets),
        total_rows=sum(item.rows for item in datasets),
        total_time_parse_success=sum(item.time_parse_success for item in datasets),
        total_time_row_number_fallback=sum(item.time_row_number_fallback for item in datasets),
        total_duplicate_line_ids=sum(item.duplicate_line_ids for item in datasets),
        total_errors=sum(len(item.errors) for item in datasets),
    )


__all__ = [
    "DatasetIngestionStats",
    "IngestionAuditReport",
    "audit_structured_csv",
    "discover_structured_csv",
    "audit_loghub_root",
]
