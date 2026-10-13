"""Deterministic incident grouping: correlation ID first, time window fallback."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta

from .models import Incident, LogEvent


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{prefix}-{digest}"


def build_incidents(
    events: list[LogEvent],
    *,
    window_seconds: int = 60,
    max_events: int = 500,
) -> list[Incident]:
    """Group events without asking an LLM to perform deterministic filtering.

    Events with a trace/request/block ID are grouped exactly by that value.
    Remaining events are split into chronological windows.  This is a useful
    baseline, not proof that every event in a window has a causal relationship.
    """

    if window_seconds <= 0 or max_events <= 0:
        raise ValueError("window_seconds and max_events must be positive")
    if not events:
        return []
    correlated: dict[str, list[LogEvent]] = defaultdict(list)
    uncorrelated: list[LogEvent] = []
    for event in events:
        if event.trace_id:
            correlated[event.trace_id].append(event)
        else:
            uncorrelated.append(event)

    incidents: list[Incident] = []
    for correlation_id, grouped in sorted(correlated.items()):
        ordered = sorted(grouped, key=lambda item: item.timestamp)
        # Very large traces are bounded so one pathological identifier cannot
        # create an unlimited Agent context.
        for index in range(0, len(ordered), max_events):
            chunk = ordered[index : index + max_events]
            incidents.append(
                Incident(
                    incident_id=_stable_id("TRACE", f"{correlation_id}:{index}"),
                    title=f"Correlated trace {correlation_id}",
                    events=chunk,
                    source=chunk[0].source,
                    metadata={"correlation_id": correlation_id, "strategy": "exact_id"},
                )
            )

    ordered_unlinked = sorted(uncorrelated, key=lambda item: item.timestamp)
    current: list[LogEvent] = []
    window = timedelta(seconds=window_seconds)
    bucket = 0
    for event in ordered_unlinked:
        should_split = current and (
            event.timestamp - current[0].timestamp > window or len(current) >= max_events
        )
        if should_split:
            incidents.append(
                Incident(
                    incident_id=_stable_id("WINDOW", f"{current[0].line_id}:{bucket}"),
                    title=f"Time window starting {current[0].timestamp.isoformat()}",
                    events=current,
                    source=current[0].source,
                    metadata={"strategy": "time_window", "window_seconds": window_seconds},
                )
            )
            bucket += 1
            current = []
        current.append(event)
    if current:
        incidents.append(
            Incident(
                incident_id=_stable_id("WINDOW", f"{current[0].line_id}:{bucket}"),
                title=f"Time window starting {current[0].timestamp.isoformat()}",
                events=current,
                source=current[0].source,
                metadata={"strategy": "time_window", "window_seconds": window_seconds},
            )
        )
    return sorted(incidents, key=lambda incident: incident.start_time or events[0].timestamp)
