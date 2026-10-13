"""Adapters that turn Loghub structured CSV files into the common event model."""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import CompressionStats, Incident, LogEvent


_TRACE_PATTERNS = (
    re.compile(r"\b(req-[0-9a-f-]{16,})\b", re.IGNORECASE),
    re.compile(r"\b(trace[-_=][A-Za-z0-9-]+)\b", re.IGNORECASE),
    re.compile(r"\b(blk_-?\d+)\b"),
)


TIME_FALLBACK_STRATEGY = "row_number_fallback"

# Strategies are source-agnostic: selection depends on field values and not a
# dataset directory name.  Every layout below was observed in the 16 bundled
# Loghub structured samples.  Layouts lacking a year (for example ``Jun 14
# 15:16:01``) are deliberately not accepted because supplying one would turn
# an ordering fallback into a fictitious wall-clock timestamp.
_DATE_TIME_STRATEGIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("date_time_iso_millis", ("Date", "Time"), "%Y-%m-%d %H:%M:%S.%f"),
    ("date_time_iso_comma_millis", ("Date", "Time"), "%Y-%m-%d %H:%M:%S,%f"),
    ("time_bgl_full_micros", ("Time",), "%Y-%m-%d-%H.%M.%S.%f"),
    ("date_time_hdfs_compact", ("Date", "Time"), "%y%m%d %H%M%S"),
    ("timestamp_iso_comma_millis", ("Timestamp",), "%Y-%m-%d %H:%M:%S,%f"),
    ("time_apache_rfc_like", ("Time",), "%a %b %d %H:%M:%S %Y"),
    ("time_healthapp_compact", ("Time",), "%Y%m%d-%H:%M:%S:%f"),
    ("date_time_spark_short", ("Date", "Time"), "%y/%m/%d %H:%M:%S"),
    ("date_time_dotted", ("Date", "Time"), "%Y.%m.%d %H:%M:%S"),
    ("date_time_iso_seconds", ("Date", "Time"), "%Y-%m-%d %H:%M:%S"),
)


def parse_time_with_strategy(
    row: dict[str, Any], row_number: int
) -> tuple[datetime, str]:
    """Parse a Loghub timestamp and report the exact strategy used.

    When no complete timestamp is present, the deterministic row-number
    fallback preserves source ordering.  Its explicit strategy label prevents
    consumers from mistaking that synthetic ordering value for real time.
    """

    for strategy, fields, pattern in _DATE_TIME_STRATEGIES:
        values = [str(row.get(field) or "").strip() for field in fields]
        if not all(values):
            continue
        candidate = " ".join(values)
        try:
            return (
                datetime.strptime(candidate, pattern).replace(tzinfo=timezone.utc),
                strategy,
            )
        except (TypeError, ValueError):
            continue

    # Numeric epoch time appears in Timestamp (BGL/Thunderbird) and Time
    # (HPC).  Restrict the range to plausible Unix seconds; interpreting a
    # compact clock such as HDFS' 203615 as an epoch would be data fabrication.
    for field_name in ("Timestamp", "Time"):
        raw = str(row.get(field_name) or "").strip()
        if not raw.isdigit() or not 100_000_000 <= int(raw) <= 9_999_999_999:
            continue
        try:
            return (
                datetime.fromtimestamp(int(raw), tz=timezone.utc),
                f"{field_name.lower()}_unix_seconds",
            )
        except (OverflowError, OSError, ValueError):
            continue

    return datetime.fromtimestamp(row_number, tz=timezone.utc), TIME_FALLBACK_STRATEGY


def _parse_time(row: dict[str, str], row_number: int) -> datetime:
    """Backward-compatible timestamp-only wrapper for internal callers."""

    return parse_time_with_strategy(row, row_number)[0]


def _trace_id(content: str) -> str | None:
    for pattern in _TRACE_PATTERNS:
        match = pattern.search(content)
        if match:
            return match.group(1)
    return None


def load_loghub_csv(path: str | Path, limit: int | None = None) -> Incident:
    """Load any `*_structured.csv` included in the Loghub repository."""

    csv_path = Path(path)
    events: list[LogEvent] = []
    with csv_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames  # consume the header to establish physical line numbers
        previous_line = reader.line_num
        for row_number, row in enumerate(reader, start=1):
            start_line, end_line = previous_line + 1, reader.line_num
            previous_line = end_line
            if limit is not None and row_number > limit:
                break
            content = row.get("Content") or row.get("Message") or ""
            component = (
                row.get("Component")
                or row.get("Process")
                or row.get("Node")
                or csv_path.parent.name
            )
            label = str(row.get("Label", "-")).strip()
            level = str(row.get("Level") or "INFO").upper()
            template = row.get("EventTemplate") or content
            template_id = row.get("EventId") or f"RAW-{row_number}"
            line_id = f"{csv_path.parent.name}-{row.get('LineId') or row_number}"
            parsed_time, time_strategy = parse_time_with_strategy(row, row_number)
            events.append(
                LogEvent(
                    line_id=line_id,
                    timestamp=parsed_time,
                    service=str(component),
                    level=level,
                    message=content,
                    template_id=str(template_id),
                    event_template=template,
                    trace_id=_trace_id(f"{content} {row.get('ADDR', '')}"),
                    source=csv_path.parent.name,
                    metadata={
                        "start_line": start_line,
                        "end_line": end_line,
                        "label": label,
                        "timestamp_parser_strategy": time_strategy,
                        "timestamp_fallback": time_strategy == TIME_FALLBACK_STRATEGY,
                    },
                )
            )
    return Incident(
        incident_id=f"LOGHUB-{csv_path.parent.name.upper()}",
        title=f"Loghub {csv_path.parent.name} sample",
        events=events,
        source="loghub",
        metadata={"path": str(csv_path.resolve())},
    )


def compression_stats(incident: Incident) -> CompressionStats:
    """Compare naive representations with a template dictionary + ID sequence.

    This is a representation experiment, not a claim about gzip compression.
    It demonstrates why an Agent should retrieve template IDs first and fetch
    full raw lines only for shortlisted evidence.
    """

    raw_chars = sum(len(event.message) for event in incident.events)
    normalized_chars = sum(
        len(json.dumps(event.model_dump(mode="json"), ensure_ascii=False))
        for event in incident.events
    )
    templates: dict[str, str] = {}
    sequence: list[str] = []
    for event in incident.events:
        templates[event.template_id] = event.event_template
        sequence.append(event.template_id)
    compact_chars = len(
        json.dumps({"templates": templates, "sequence": sequence}, ensure_ascii=False)
    )
    return CompressionStats(
        rows=len(incident.events),
        unique_templates=len(templates),
        raw_characters=raw_chars,
        normalized_json_characters=normalized_chars,
        compact_characters=compact_chars,
        compact_vs_raw_ratio=round(compact_chars / max(raw_chars, 1), 4),
        compact_vs_json_ratio=round(compact_chars / max(normalized_chars, 1), 4),
    )
