from __future__ import annotations

import json

import pytest

from logrca.blind_data import (
    DATASET_VERSION,
    build_blind_dataset,
    export_blind_dataset,
    incidents_for_split,
    verify_manifest,
)
from logrca.models import GroundTruth, LogEvent


def test_generator_is_deterministic_and_manifest_is_verifiable() -> None:
    first = build_blind_dataset(case_count=100, seed=73)
    second = build_blind_dataset(case_count=100, seed=73)
    changed = build_blind_dataset(case_count=100, seed=74)

    assert first == second
    assert first.manifest.case_count == 100
    assert first.manifest.dataset_version == DATASET_VERSION
    assert verify_manifest(first)
    assert first.manifest.content_sha256 != changed.manifest.content_sha256


def test_dataset_has_eight_known_faults_normal_and_ood() -> None:
    artifacts = build_blind_dataset(case_count=120)
    labels = list(artifacts.truth_sidecar.labels.values())
    known_faults = {
        label.root_cause_code
        for label in labels
        if label.is_anomaly and label.root_cause_code is not None
    }

    assert len(known_faults) >= 8
    assert any(not label.is_anomaly for label in labels)
    assert any(label.is_anomaly and label.root_cause_code is None for label in labels)
    assert all(isinstance(label, GroundTruth) for label in labels)
    assert sum(artifacts.manifest.scenario_counts.values()) == 120


def test_agent_corpus_has_no_truth_fields_or_identity_leakage() -> None:
    artifacts = build_blind_dataset(case_count=100, seed=991)
    corpus_json = json.dumps(artifacts.corpus.model_dump(mode="json"), ensure_ascii=False).lower()

    assert "root_cause_code" not in corpus_json
    assert "evidence_ids" not in corpus_json
    assert "fault_family" not in corpus_json
    assert "perturbations" not in corpus_json
    for incident in artifacts.corpus.incidents:
        label = artifacts.truth_sidecar.labels[incident.incident_id]
        identity = " ".join(
            [
                incident.incident_id,
                incident.title,
                incident.source,
                *[event.line_id for event in incident.events],
                *[event.source for event in incident.events],
                *[event.template_id for event in incident.events],
            ]
        ).lower()
        assert label.fault_family.lower() not in identity
        if label.root_cause_code:
            assert label.root_cause_code.lower() not in identity

    # Service topology is legitimate evidence in real systems, but this
    # developer-authored benchmark must not assign a unique service pair to
    # each answer class.  Every family therefore shares both neutral services.
    services_by_family: dict[str, set[str]] = {}
    for incident in artifacts.corpus.incidents:
        family = artifacts.truth_sidecar.labels[incident.incident_id].fault_family
        services_by_family.setdefault(family, set()).update(
            event.service
            for event in incident.events
            if event.service.startswith("application-worker-")
        )
    assert all(
        services == {"application-worker-a", "application-worker-b"}
        for services in services_by_family.values()
    )

    # The old sequential three-minute schedule leaked family through minute
    # modulo.  Hashed deterministic timestamps must not preserve that pattern.
    starts = [incident.start_time for incident in artifacts.corpus.incidents]
    deltas = [
        (right - left).total_seconds()
        for left, right in zip(starts, starts[1:])
        if left is not None and right is not None
    ]
    assert len(set(deltas)) > 10


def test_grouped_split_has_no_cross_split_leakage() -> None:
    artifacts = build_blind_dataset(case_count=120, seed=7)
    seen: dict[str, str] = {}
    for label in artifacts.truth_sidecar.labels.values():
        existing = seen.setdefault(label.group_id, label.split)
        assert existing == label.split

    assert set(artifacts.manifest.split_counts) == {"train", "dev", "test"}
    assert all(count > 0 for count in artifacts.manifest.split_counts.values())
    assert sum(artifacts.manifest.split_counts.values()) == 120
    families = {label.fault_family for label in artifacts.truth_sidecar.labels.values()}
    for split in ("train", "dev", "test"):
        visible = incidents_for_split(artifacts, split)
        assert len(visible) == artifacts.manifest.split_counts[split]
        assert all(
            artifacts.truth_sidecar.labels[item.incident_id].split == split
            for item in visible
        )
        split_families = {
            label.fault_family
            for label in artifacts.truth_sidecar.labels.values()
            if label.split == split
        }
        assert split_families == families

    # Exact root-evidence messages and their template IDs must not appear in a
    # different split.  Service-only grouping would pass the group-id check
    # above while still leaking the same wording into train and test.
    evidence_messages: dict[str, set[str]] = {
        "train": set(),
        "dev": set(),
        "test": set(),
    }
    evidence_templates: dict[str, set[str]] = {
        "train": set(),
        "dev": set(),
        "test": set(),
    }
    for incident in artifacts.corpus.incidents:
        label = artifacts.truth_sidecar.labels[incident.incident_id]
        expected_ids = set(label.evidence_ids)
        for event in incident.events:
            if event.line_id in expected_ids:
                evidence_messages[label.split].add(event.message)
                evidence_templates[label.split].add(event.template_id)
    for left, right in (("train", "dev"), ("train", "test"), ("dev", "test")):
        assert evidence_messages[left].isdisjoint(evidence_messages[right])
        assert evidence_templates[left].isdisjoint(evidence_templates[right])


def test_noise_and_all_supported_perturbations_are_exercised() -> None:
    background = [
        LogEvent(
            line_id="labeled-anomaly-row",
            timestamp="2026-01-01T00:00:00Z",
            service="real-log-service",
            level="WARN",
            message="Real Loghub background message",
            template_id="ROOT_CAUSE_LABEL_9",
            event_template="Real Loghub background message",
            source="BGL-anomaly",
            metadata={"label": "anomaly", "root": "secret"},
        )
    ]
    artifacts = build_blind_dataset(
        case_count=120, seed=20260921, background_messages=background
    )
    visible = json.dumps(artifacts.corpus.model_dump(mode="json"), ensure_ascii=False)
    perturbations = artifacts.manifest.perturbation_counts

    assert "Real Loghub background message" in visible
    assert "labeled-anomaly-row" not in visible
    assert "ROOT_CAUSE_LABEL_9" not in visible
    assert "BGL-anomaly" not in visible
    assert '"label": "anomaly"' not in visible
    assert {
        "out_of_order",
        "duplicate_event",
        "missing_evidence",
        "wrong_trace",
        "distractor_fault",
    } <= set(perturbations)
    assert all(count > 0 for count in perturbations.values())
    assert sum(artifacts.manifest.trace_corruption_role_counts.values()) == perturbations[
        "wrong_trace"
    ]

    # Perturbations must not be accidentally correlated with one fault family.
    labels = list(artifacts.truth_sidecar.labels.values())
    for perturbation in (
        "out_of_order",
        "duplicate_event",
        "missing_evidence",
        "wrong_trace",
        "distractor_fault",
    ):
        exposed_families = {
            label.fault_family
            for label in labels
            if perturbation in label.perturbations
        }
        assert len(exposed_families) >= 5
        exposed_splits = {
            label.split
            for label in labels
            if perturbation in label.perturbations
        }
        assert exposed_splits == {"train", "dev", "test"}

    trace_labels = [
        label for label in labels if "wrong_trace" in label.perturbations
    ]
    assert all(label.trace_corruption_role is not None for label in trace_labels)
    assert all(
        label.trace_corruption_role in {"direct", "support"}
        for label in trace_labels
        if label.is_anomaly
    )
    assert all(
        label.trace_corruption_role == "non_evidence"
        for label in trace_labels
        if not label.is_anomaly
    )


def test_export_keeps_truth_sidecar_physically_separate(tmp_path) -> None:
    artifacts = build_blind_dataset(case_count=100, seed=19)
    paths = export_blind_dataset(artifacts, tmp_path)

    assert set(paths) == {"corpus", "truth", "manifest"}
    corpus_text = paths["corpus"].read_text(encoding="utf-8")
    truth_text = paths["truth"].read_text(encoding="utf-8")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert "root_cause_code" not in corpus_text
    assert "root_cause_code" in truth_text
    assert manifest["content_sha256"] == artifacts.manifest.content_sha256


def test_generator_rejects_tiny_benchmark() -> None:
    with pytest.raises(ValueError, match="at least 100"):
        build_blind_dataset(case_count=99)
