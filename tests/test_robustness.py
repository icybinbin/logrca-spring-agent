from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from logrca.agents import IncidentManager
from logrca.models import Incident, LogEvent
from logrca.robustness import (
    delete_direct_evidence,
    duplicate_events,
    insert_decoys,
    keep_only_symptoms,
    opaque_identifiers,
    perturb_messages,
    shuffle_events,
    truncate_events,
)
from logrca.synthetic import build_scenario


BASE_TIME = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _event(
    number: int,
    *,
    level: str,
    template_id: str,
    message: str,
    service: str = "order-service",
    trace_id: str | None = "trace-customer-42",
) -> LogEvent:
    return LogEvent(
        line_id=f"line-sensitive-{number}",
        timestamp=BASE_TIME + timedelta(seconds=number),
        service=service,
        level=level,
        message=message,
        template_id=template_id,
        event_template=message,
        trace_id=trace_id,
        source="test-source",
        metadata={"nested": {"trace": trace_id, "line": f"line-sensitive-{number}"}},
    )


@pytest.fixture
def incident() -> Incident:
    return Incident(
        incident_id="incident-sensitive-99",
        title="Failure for incident-sensitive-99",
        source="test",
        metadata={"correlation_key": "trace-customer-42"},
        events=[
            _event(
                1,
                level="INFO",
                template_id="REQUEST_RECEIVED",
                message="Request accepted for order=42",
            ),
            _event(
                2,
                level="ERROR",
                template_id="DIRECT_FAILURE_CODE",
                message="Connection refused after request timed out after 30000ms",
                service="database-client",
            ),
            _event(
                3,
                level="ERROR",
                template_id="UPSTREAM_5XX",
                message="order-service returned HTTP 503 after retry failed",
                service="gateway",
            ),
            _event(
                4,
                level="WARN",
                template_id="RETRY",
                message="Client retry scheduled after upstream failure",
                service="gateway",
            ),
        ],
    )


def test_robustness_module_has_no_diagnosis_or_truth_imports() -> None:
    """The transform layer must not secretly learn labels from project code."""

    source_path = Path(__file__).parents[1] / "logrca" / "robustness.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any(name.endswith("agents") for name in imported_modules)
    assert not any(name.endswith("synthetic") for name in imported_modules)
    assert not any(name.endswith("evaluation") for name in imported_modules)


def test_opaque_identifiers_are_deterministic_and_leave_no_visible_ids(
    incident: Incident,
) -> None:
    original = incident.model_copy(deep=True)
    first = opaque_identifiers(
        incident, seed=704, include_template_ids=True, include_services=True
    )
    second = opaque_identifiers(
        incident, seed=704, include_template_ids=True, include_services=True
    )
    other_seed = opaque_identifiers(
        incident, seed=705, include_template_ids=True, include_services=True
    )

    assert first == second
    assert first.incident.incident_id != other_seed.incident.incident_id
    assert incident == original  # transforms never mutate the source fixture

    visible = json.dumps(first.incident.model_dump(mode="json"), ensure_ascii=False)
    private_values = {
        incident.incident_id,
        *(event.line_id for event in incident.events),
        *(event.template_id for event in incident.events),
        *(event.trace_id for event in incident.events if event.trace_id),
        *(event.service for event in incident.events),
    }
    assert all(value not in visible for value in private_values)
    assert len({event.line_id for event in first.incident.events}) == len(incident.events)
    assert len({event.trace_id for event in first.incident.events}) == 1
    assert first.metadata.remap_line_ids(["line-sensitive-2"], strict=True) == [
        first.metadata.mappings["line_id"]["line-sensitive-2"]
    ]
    with pytest.raises(KeyError):
        first.metadata.remap_line_ids(["not-present"], strict=True)


def test_message_perturbation_is_seeded_and_changes_both_text_views(
    incident: Incident,
) -> None:
    first = perturb_messages(incident, seed=19, intensity=1.0)
    second = perturb_messages(incident, seed=19, intensity=1.0)

    assert first == second
    assert first.metadata.parameters["replacement_count"] > 0
    assert first.metadata.parameters["changed_line_ids"]
    assert [event.line_id for event in first.incident.events] == [
        event.line_id for event in incident.events
    ]
    assert first.incident.events[1].message != incident.events[1].message
    assert first.incident.events[1].event_template != incident.events[1].event_template
    assert first.incident.events[1].template_id == incident.events[1].template_id


def test_surface_perturbation_validates_intensity(incident: Incident) -> None:
    with pytest.raises(ValueError, match="intensity"):
        perturb_messages(incident, seed=1, intensity=1.1)


def test_delete_evidence_uses_only_explicit_ids_and_records_missing(
    incident: Incident,
) -> None:
    result = delete_direct_evidence(
        incident, ["line-sensitive-2", "private-label-does-not-exist"], seed=12
    )

    assert [event.line_id for event in result.incident.events] == [
        "line-sensitive-1",
        "line-sensitive-3",
        "line-sensitive-4",
    ]
    assert result.metadata.removed_line_ids == ["line-sensitive-2"]
    assert result.metadata.parameters["requested_but_missing"] == [
        "private-label-does-not-exist"
    ]
    assert result.metadata.after_event_count == 3


def test_keep_only_symptoms_preserves_source_order(incident: Incident) -> None:
    result = keep_only_symptoms(
        incident,
        ["line-sensitive-4", "line-sensitive-3"],
        seed=13,
    )

    assert [event.line_id for event in result.incident.events] == [
        "line-sensitive-3",
        "line-sensitive-4",
    ]
    assert result.metadata.removed_line_ids == [
        "line-sensitive-1",
        "line-sensitive-2",
    ]


def test_decoys_are_plausibly_interleaved_but_labeled_only_in_sidecar(
    incident: Incident,
) -> None:
    decoy = _event(
        77,
        level="ERROR",
        template_id="UNRELATED_ERROR",
        message="Unrelated maintenance operation failed",
        service="email-service",
        trace_id="trace-from-other-incident",
    )
    first = insert_decoys(incident, [decoy], seed=81)
    second = insert_decoys(incident, [decoy], seed=81)

    assert first == second
    assert len(first.incident.events) == len(incident.events) + 1
    inserted_id = first.metadata.inserted_line_ids[0]
    inserted = next(event for event in first.incident.events if event.line_id == inserted_id)
    assert incident.start_time <= inserted.timestamp <= incident.end_time
    assert inserted.trace_id == "trace-customer-42"
    assert inserted.line_id != decoy.line_id
    assert len({event.line_id for event in first.incident.events}) == len(
        first.incident.events
    )
    # No Agent-visible metadata identifies the distractor as a decoy.
    visible = json.dumps(first.incident.model_dump(mode="json"), ensure_ascii=False).lower()
    assert "decoy" not in visible


def test_shuffle_changes_only_ingestion_order(incident: Incident) -> None:
    first = shuffle_events(incident, seed=91)
    second = shuffle_events(incident, seed=91)

    assert first == second
    assert first.metadata.parameters["before_order"] != first.metadata.parameters["after_order"]
    assert {event.line_id for event in first.incident.events} == {
        event.line_id for event in incident.events
    }
    before_payload = {
        event.line_id: event.model_dump(mode="json") for event in incident.events
    }
    after_payload = {
        event.line_id: event.model_dump(mode="json")
        for event in first.incident.events
    }
    assert before_payload == after_payload


def test_duplicate_events_use_fresh_ids_and_record_provenance(
    incident: Incident,
) -> None:
    result = duplicate_events(incident, seed=101, count=3)

    assert len(result.incident.events) == len(incident.events) + 3
    assert len(set(result.metadata.inserted_line_ids)) == 3
    assert len({event.line_id for event in result.incident.events}) == len(
        result.incident.events
    )
    original_by_id = {event.line_id: event for event in incident.events}
    original_to_duplicate = result.metadata.mappings["duplicate_line_id"]
    for source_key, duplicate_id in original_to_duplicate.items():
        original_id = source_key.rsplit("#", 1)[0]
        duplicate = next(
            event for event in result.incident.events if event.line_id == duplicate_id
        )
        assert duplicate.model_copy(update={"line_id": original_id}) == original_by_id[
            original_id
        ]


@pytest.mark.parametrize(
    ("keep", "expected"),
    [
        ("head", ["line-sensitive-1", "line-sensitive-2"]),
        ("tail", ["line-sensitive-3", "line-sensitive-4"]),
    ],
)
def test_truncate_head_and_tail(
    incident: Incident, keep: str, expected: list[str]
) -> None:
    result = truncate_events(incident, seed=111, max_events=2, keep=keep)  # type: ignore[arg-type]
    assert [event.line_id for event in result.incident.events] == expected
    assert len(result.metadata.removed_line_ids) == 2


def test_random_window_and_empty_truncation_are_deterministic(
    incident: Incident,
) -> None:
    first = truncate_events(incident, seed=121, max_events=2, keep="window")
    second = truncate_events(incident, seed=121, max_events=2, keep="window")
    empty = truncate_events(incident, seed=121, max_events=0, keep="tail")

    assert first == second
    assert len(first.incident.events) == 2
    assert empty.incident.events == []
    with pytest.raises(ValueError, match="non-negative"):
        truncate_events(incident, seed=1, max_events=-1)


def test_counterfactual_removes_the_current_fixed_template_answer() -> None:
    """A hidden evaluator can use truth IDs without exposing them to a transform."""

    case = build_scenario("db_pool_exhausted")
    stripped = delete_direct_evidence(
        case.incident, case.truth.evidence_ids, seed=131
    ).incident
    opaque = opaque_identifiers(case.incident, seed=132).incident

    stripped_report = IncidentManager().diagnose(stripped, mode="multi")
    opaque_report = IncidentManager().diagnose(opaque, mode="multi")
    assert stripped_report.root_cause_code != case.truth.root_cause_code
    assert opaque_report.is_anomaly is True
    # Opaque IDs prevent identity leakage but preserve legitimate message
    # evidence, so a semantic policy should reach the same conclusion.
    assert opaque_report.root_cause_code == case.truth.root_cause_code
