"""Deterministic, label-agnostic robustness transforms for log incidents.

This module deliberately knows nothing about the diagnosis implementation,
its cause catalogue, or synthetic ground truth.  A benchmark *driver* may
select event IDs from a private label file and pass those IDs here, but the
transform itself only performs the requested mechanical operation.

Every function returns a :class:`TransformResult`.  Its ``metadata`` is an
evaluation-only sidecar: it contains ID mappings and the exact mutation that
was applied, so it must never be included in the incident shown to an Agent.
Keeping the sidecar separate makes blind and counterfactual tests repeatable
without putting an answer into Agent-visible fields.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import timedelta
from typing import Any, Iterable, Literal, Sequence

from pydantic import BaseModel, Field

from .models import Incident, LogEvent


class TransformationMetadata(BaseModel):
    """Private, reproducible description of one incident mutation.

    ``mappings`` always points from the original value to the transformed
    value.  It is intentionally kept outside ``Incident.metadata`` because an
    Agent must not receive the reverse lookup table during a blind test.
    """

    name: str
    seed: int
    before_event_count: int
    after_event_count: int
    before_fingerprint: str
    after_fingerprint: str
    mappings: dict[str, dict[str, str]] = Field(default_factory=dict)
    removed_line_ids: list[str] = Field(default_factory=list)
    inserted_line_ids: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)

    def remap_line_ids(self, line_ids: Iterable[str], *, strict: bool = False) -> list[str]:
        """Remap private evidence labels after an opaque-ID transform.

        This helper operates on ordinary strings rather than importing a
        ground-truth model.  The benchmark owns the labels and decides whether
        and when to call it.
        """

        mapping = self.mappings.get("line_id", {})
        result: list[str] = []
        for line_id in line_ids:
            if strict and line_id not in mapping:
                raise KeyError(f"No transformed line_id for {line_id!r}")
            result.append(mapping.get(line_id, line_id))
        return result


class TransformResult(BaseModel):
    """A transformed Agent-visible incident plus evaluator-only metadata."""

    incident: Incident
    metadata: TransformationMetadata


def _fingerprint(incident: Incident) -> str:
    payload = json.dumps(
        incident.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _opaque_id(seed: int, namespace: str, value: str) -> str:
    digest = hashlib.blake2b(
        f"{seed}\x00{namespace}\x00{value}".encode("utf-8"), digest_size=10
    ).hexdigest()
    prefixes = {
        "incident_id": "inc",
        "line_id": "evt",
        "template_id": "tpl",
        "trace_id": "trc",
        "service": "svc",
    }
    return f"{prefixes[namespace]}_{digest}"


def _unique_mapping(values: Iterable[str], seed: int, namespace: str) -> dict[str, str]:
    # Sorting makes a mapping independent of the current event order.  This is
    # important when opaque IDs are applied after an order perturbation.
    return {
        value: _opaque_id(seed, namespace, value)
        for value in sorted(set(values))
        if value
    }


def _deep_replace(value: Any, replacements: dict[str, str]) -> Any:
    """Replace known IDs in free text and JSON-like metadata recursively."""

    if isinstance(value, str):
        updated = value
        # A longer identifier must be replaced before one that is its prefix.
        for original in sorted(replacements, key=len, reverse=True):
            updated = updated.replace(original, replacements[original])
        return updated
    if isinstance(value, list):
        return [_deep_replace(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_deep_replace(item, replacements) for item in value)
    if isinstance(value, dict):
        return {
            _deep_replace(key, replacements): _deep_replace(item, replacements)
            for key, item in value.items()
        }
    return value


def _result(
    before: Incident,
    after: Incident,
    *,
    name: str,
    seed: int,
    mappings: dict[str, dict[str, str]] | None = None,
    removed_line_ids: Iterable[str] = (),
    inserted_line_ids: Iterable[str] = (),
    parameters: dict[str, Any] | None = None,
) -> TransformResult:
    return TransformResult(
        incident=after,
        metadata=TransformationMetadata(
            name=name,
            seed=seed,
            before_event_count=len(before.events),
            after_event_count=len(after.events),
            before_fingerprint=_fingerprint(before),
            after_fingerprint=_fingerprint(after),
            mappings=mappings or {},
            removed_line_ids=list(removed_line_ids),
            inserted_line_ids=list(inserted_line_ids),
            parameters=parameters or {},
        ),
    )


def opaque_identifiers(
    incident: Incident,
    *,
    seed: int,
    include_template_ids: bool = True,
    include_services: bool = False,
) -> TransformResult:
    """Replace meaningful identifiers with stable, seed-derived opaque IDs.

    Line, incident and trace IDs are always replaced.  Template IDs default to
    opaque values because they frequently encode a root cause in benchmark
    fixtures.  Services remain readable by default because service topology is
    legitimate diagnostic evidence; callers can hide those names as a harder
    counterfactual with ``include_services=True``.
    """

    mappings: dict[str, dict[str, str]] = {
        "incident_id": _unique_mapping([incident.incident_id], seed, "incident_id"),
        "line_id": _unique_mapping(
            (event.line_id for event in incident.events), seed, "line_id"
        ),
        "trace_id": _unique_mapping(
            (event.trace_id or "" for event in incident.events), seed, "trace_id"
        ),
    }
    if include_template_ids:
        mappings["template_id"] = _unique_mapping(
            (event.template_id for event in incident.events), seed, "template_id"
        )
    if include_services:
        mappings["service"] = _unique_mapping(
            (event.service for event in incident.events), seed, "service"
        )

    flat_replacements = {
        original: transformed
        for mapping in mappings.values()
        for original, transformed in mapping.items()
    }
    events: list[LogEvent] = []
    for event in incident.events:
        events.append(
            event.model_copy(
                deep=True,
                update={
                    "line_id": mappings["line_id"][event.line_id],
                    "template_id": mappings.get("template_id", {}).get(
                        event.template_id, event.template_id
                    ),
                    "trace_id": mappings["trace_id"].get(
                        event.trace_id or "", event.trace_id
                    ),
                    "service": mappings.get("service", {}).get(
                        event.service, event.service
                    ),
                    "message": _deep_replace(event.message, flat_replacements),
                    "event_template": _deep_replace(
                        event.event_template, flat_replacements
                    ),
                    "metadata": _deep_replace(event.metadata, flat_replacements),
                },
            )
        )

    transformed = incident.model_copy(
        deep=True,
        update={
            "incident_id": mappings["incident_id"][incident.incident_id],
            "title": _deep_replace(incident.title, flat_replacements),
            "events": events,
            "metadata": _deep_replace(incident.metadata, flat_replacements),
        },
    )
    return _result(
        incident,
        transformed,
        name="opaque_identifiers",
        seed=seed,
        mappings=mappings,
        parameters={
            "include_template_ids": include_template_ids,
            "include_services": include_services,
        },
    )


# These are generic operational paraphrases, not mappings from a symptom to a
# root cause.  Long phrases are tried first to prevent a shorter phrase from
# consuming part of a more specific surface form.
_PARAPHRASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("request timed out", ("operation exceeded its time limit", "request hit its deadline")),
    ("connection is not available", ("no connection could be obtained", "a connection was unavailable")),
    ("connection refused", ("the endpoint rejected the connection", "the connection attempt was rejected")),
    ("request accepted", ("operation received", "request received")),
    ("returned", ("responded with", "produced")),
    ("unable to", ("could not", "was not able to")),
    ("failed", ("did not complete successfully", "was unsuccessful")),
    ("completed", ("finished", "concluded")),
    ("started", ("began", "was initiated")),
    ("scheduled", ("queued", "planned")),
    ("after", ("following", "subsequent to")),
)


def _match_case(replacement: str, matched: str) -> str:
    if matched.isupper():
        return replacement.upper()
    if matched[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _perturb_text(text: str, rng: random.Random, intensity: float) -> tuple[str, int]:
    updated = text
    changes = 0
    for phrase, alternatives in _PARAPHRASES:
        pattern = re.compile(re.escape(phrase), flags=re.IGNORECASE)

        def replace(match: re.Match[str]) -> str:
            nonlocal changes
            if rng.random() > intensity:
                return match.group(0)
            changes += 1
            return _match_case(rng.choice(alternatives), match.group(0))

        updated = pattern.sub(replace, updated)

    # Harmless formatting variation exercises brittle exact-string matching.
    if intensity > 0 and rng.random() <= intensity:
        reformatted = re.sub(r"\s*=\s*", " = ", updated)
        reformatted = re.sub(r"(?<=\d)(ms|s)\b", r" \1", reformatted)
        if reformatted != updated:
            changes += 1
            updated = reformatted
    return updated, changes


def perturb_messages(
    incident: Incident,
    *,
    seed: int,
    intensity: float = 1.0,
) -> TransformResult:
    """Apply meaning-preserving lexical and formatting surface variation."""

    if not 0.0 <= intensity <= 1.0:
        raise ValueError("intensity must be between 0.0 and 1.0")
    rng = random.Random(seed)
    changed_line_ids: list[str] = []
    replacement_count = 0
    events: list[LogEvent] = []
    for event in incident.events:
        message, message_changes = _perturb_text(event.message, rng, intensity)
        template, template_changes = _perturb_text(event.event_template, rng, intensity)
        if message != event.message or template != event.event_template:
            changed_line_ids.append(event.line_id)
        replacement_count += message_changes + template_changes
        events.append(
            event.model_copy(
                deep=True,
                update={"message": message, "event_template": template},
            )
        )
    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="perturb_messages",
        seed=seed,
        parameters={
            "intensity": intensity,
            "changed_line_ids": changed_line_ids,
            "replacement_count": replacement_count,
        },
    )


def delete_direct_evidence(
    incident: Incident,
    direct_evidence_ids: Iterable[str],
    *,
    seed: int,
) -> TransformResult:
    """Delete caller-selected direct evidence without inspecting its meaning."""

    requested = set(direct_evidence_ids)
    existing = {event.line_id for event in incident.events}
    removed = [event.line_id for event in incident.events if event.line_id in requested]
    events = [
        event.model_copy(deep=True)
        for event in incident.events
        if event.line_id not in requested
    ]
    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="delete_direct_evidence",
        seed=seed,
        removed_line_ids=removed,
        parameters={
            "requested_line_ids": sorted(requested),
            "requested_but_missing": sorted(requested - existing),
        },
    )


def keep_only_symptoms(
    incident: Incident,
    symptom_ids: Iterable[str],
    *,
    seed: int,
) -> TransformResult:
    """Keep only caller-selected symptoms; no symptom classifier is embedded."""

    requested = set(symptom_ids)
    existing = {event.line_id for event in incident.events}
    events = [
        event.model_copy(deep=True)
        for event in incident.events
        if event.line_id in requested
    ]
    kept = {event.line_id for event in events}
    removed = [event.line_id for event in incident.events if event.line_id not in kept]
    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="keep_only_symptoms",
        seed=seed,
        removed_line_ids=removed,
        parameters={
            "requested_symptom_ids": sorted(requested),
            "requested_but_missing": sorted(requested - existing),
        },
    )


def insert_decoys(
    incident: Incident,
    decoys: Sequence[LogEvent],
    *,
    seed: int,
    align_timestamps: bool = True,
    attach_existing_trace: bool = True,
) -> TransformResult:
    """Insert caller-provided distractor events with fresh line IDs.

    Decoys are not marked inside the returned incident.  Their IDs live only
    in the private sidecar, otherwise the transform itself would reveal which
    evidence the Agent should ignore.
    """

    rng = random.Random(seed)
    events = [event.model_copy(deep=True) for event in incident.events]
    used_ids = {event.line_id for event in events}
    trace_ids = sorted({event.trace_id for event in events if event.trace_id})
    start = incident.start_time
    end = incident.end_time
    span_seconds = (end - start).total_seconds() if start and end else 0.0
    inserted: list[str] = []
    source_mapping: dict[str, str] = {}

    for index, decoy in enumerate(decoys):
        source_key = f"{decoy.line_id}#{index}"
        candidate = _opaque_id(seed, "line_id", f"decoy:{source_key}")
        attempt = 0
        while candidate in used_ids:
            attempt += 1
            candidate = _opaque_id(
                seed, "line_id", f"decoy:{source_key}:{attempt}"
            )
        used_ids.add(candidate)
        inserted.append(candidate)
        source_mapping[source_key] = candidate

        timestamp = decoy.timestamp
        if align_timestamps and start is not None:
            if span_seconds > 0:
                timestamp = start + timedelta(seconds=rng.random() * span_seconds)
            else:
                timestamp = start + timedelta(microseconds=index + 1)
        trace_id = decoy.trace_id
        if attach_existing_trace and trace_ids:
            trace_id = rng.choice(trace_ids)
        clone = decoy.model_copy(
            deep=True,
            update={
                "line_id": candidate,
                "timestamp": timestamp,
                "trace_id": trace_id,
            },
        )
        # Random list position represents ingestion interleaving.  A consumer
        # that needs causal order should sort by the event timestamp.
        events.insert(rng.randrange(len(events) + 1), clone)

    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="insert_decoys",
        seed=seed,
        mappings={"decoy_source_line_id": source_mapping},
        inserted_line_ids=inserted,
        parameters={
            "align_timestamps": align_timestamps,
            "attach_existing_trace": attach_existing_trace,
        },
    )


def shuffle_events(incident: Incident, *, seed: int) -> TransformResult:
    """Deterministically scramble ingestion order without changing timestamps."""

    rng = random.Random(seed)
    events = [event.model_copy(deep=True) for event in incident.events]
    before_order = [event.line_id for event in events]
    rng.shuffle(events)
    if len(events) > 1 and [event.line_id for event in events] == before_order:
        events = events[1:] + events[:1]
    after_order = [event.line_id for event in events]
    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="shuffle_events",
        seed=seed,
        parameters={"before_order": before_order, "after_order": after_order},
    )


def duplicate_events(
    incident: Incident,
    *,
    seed: int,
    count: int = 1,
) -> TransformResult:
    """Duplicate random events using new IDs, simulating at-least-once delivery."""

    if count < 0:
        raise ValueError("count must be non-negative")
    if count and not incident.events:
        raise ValueError("cannot duplicate events in an empty incident")
    rng = random.Random(seed)
    events = [event.model_copy(deep=True) for event in incident.events]
    inserted: list[str] = []
    duplicate_mapping: dict[str, str] = {}
    for index in range(count):
        original = rng.choice(incident.events)
        duplicate_id = _opaque_id(
            seed, "line_id", f"duplicate:{original.line_id}:{index}"
        )
        duplicate = original.model_copy(deep=True, update={"line_id": duplicate_id})
        insert_at = rng.randrange(len(events) + 1)
        events.insert(insert_at, duplicate)
        inserted.append(duplicate_id)
        duplicate_mapping[f"{original.line_id}#{index}"] = duplicate_id
    transformed = incident.model_copy(deep=True, update={"events": events})
    return _result(
        incident,
        transformed,
        name="duplicate_events",
        seed=seed,
        mappings={"duplicate_line_id": duplicate_mapping},
        inserted_line_ids=inserted,
        parameters={"count": count},
    )


def truncate_events(
    incident: Incident,
    *,
    seed: int,
    max_events: int,
    keep: Literal["head", "tail", "window"] = "tail",
) -> TransformResult:
    """Keep a deterministic prefix, suffix, or contiguous random window."""

    if max_events < 0:
        raise ValueError("max_events must be non-negative")
    if keep not in {"head", "tail", "window"}:
        raise ValueError("keep must be 'head', 'tail', or 'window'")

    total = len(incident.events)
    size = min(max_events, total)
    if keep == "head":
        start = 0
    elif keep == "tail":
        start = total - size
    else:
        start = random.Random(seed).randrange(total - size + 1) if size < total else 0
    selected = incident.events[start : start + size]
    kept_ids = {event.line_id for event in selected}
    removed = [event.line_id for event in incident.events if event.line_id not in kept_ids]
    transformed = incident.model_copy(
        deep=True,
        update={"events": [event.model_copy(deep=True) for event in selected]},
    )
    return _result(
        incident,
        transformed,
        name="truncate_events",
        seed=seed,
        removed_line_ids=removed,
        parameters={"max_events": max_events, "keep": keep, "start_index": start},
    )


__all__ = [
    "TransformationMetadata",
    "TransformResult",
    "delete_direct_evidence",
    "duplicate_events",
    "insert_decoys",
    "keep_only_symptoms",
    "opaque_identifiers",
    "perturb_messages",
    "shuffle_events",
    "truncate_events",
]
