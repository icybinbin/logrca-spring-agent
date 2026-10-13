"""Deterministic, leakage-resistant benchmark data for log RCA Agents.

This module deliberately does *not* import the rule catalogue used by the
offline Agent.  That prevents direct lookup-table reuse, although the corpus
and implementation remain developer-authored in one project and therefore do
not constitute an independent external evaluation.

The public API returns three separate artifacts:

``BlindCorpus``
    Agent-visible incidents.  It contains opaque IDs and no labels.
``BlindTruthSidecar``
    Evaluation-only labels, evidence IDs, split membership and perturbations.
``BlindManifest``
    Reproducibility metadata and a SHA-256 digest covering both artifacts.

Real Loghub messages can be supplied as background noise.  Labels and source
metadata from those events are intentionally discarded before they enter the
Agent-visible corpus.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, NamedTuple, Sequence

from pydantic import BaseModel, Field

from .models import GroundTruth, Incident, LogEvent


DATASET_VERSION = "blind-rca-v1.3"
DEFAULT_CASE_COUNT = 120
DEFAULT_SEED = 20260921
DatasetSplit = Literal["train", "dev", "test"]


class BlindCorpus(BaseModel):
    """The only benchmark artifact that may be passed to an Agent."""

    dataset_version: str = DATASET_VERSION
    incidents: list[Incident]


class BlindTruthRecord(GroundTruth):
    """One evaluation label; this model is compatible with ``GroundTruth``."""

    fault_family: str
    template_variant: str
    root_service: str
    split: DatasetSplit
    group_id: str
    perturbations: list[str] = Field(default_factory=list)
    trace_corruption_role: Literal["direct", "support", "non_evidence"] | None = None


class BlindTruthSidecar(BaseModel):
    """Evaluation-only labels keyed by opaque incident ID."""

    dataset_version: str = DATASET_VERSION
    labels: dict[str, BlindTruthRecord]


class BlindManifest(BaseModel):
    """Small deterministic manifest used to reproduce and audit a corpus."""

    dataset_version: str = DATASET_VERSION
    seed: int
    case_count: int
    group_count: int
    background_pool_size: int
    split_counts: dict[str, int]
    scenario_counts: dict[str, int]
    perturbation_counts: dict[str, int]
    trace_corruption_role_counts: dict[str, int]
    content_sha256: str


class BlindDatasetArtifacts(NamedTuple):
    """Convenient container whose members are still separately serializable."""

    corpus: BlindCorpus
    truth_sidecar: BlindTruthSidecar
    manifest: BlindManifest


@dataclass(frozen=True)
class _Variant:
    direct: str
    support: str


@dataclass(frozen=True)
class _Blueprint:
    family: str
    root_cause_code: str | None
    variants: tuple[_Variant, _Variant, _Variant]
    is_anomaly: bool = True
    is_ood: bool = False


@dataclass(frozen=True)
class _DraftEvent:
    timestamp: datetime
    service: str
    level: str
    message: str
    trace_id: str | None
    is_evidence: bool = False
    evidence_role: Literal["direct", "support"] | None = None


# These phrases are an independently authored benchmark specification.  They
# intentionally differ from the exact template IDs in agents.py.  A model or
# retrieval policy must reason from the log text rather than exploit a shared
# symbolic label.
_BLUEPRINTS: tuple[_Blueprint, ...] = (
    _Blueprint(
        family="relational_database_pressure",
        root_cause_code="db_pool_exhausted",
        variants=(
            _Variant(
                "Connection lease exceeded 30000 ms; pool active=40 idle=0 pending=76",
                "Transaction aborted because no JDBC connection became available",
            ),
            _Variant(
                "Datasource wait queue reached 128 while every connection was in use",
                "Repository operation failed after connection acquisition deadline",
            ),
            _Variant(
                "Pool housekeeper reports total=32 active=32 idle=0 waiting=91",
                "SQL request cancelled before execution: datasource checkout timed out",
            ),
        ),
    ),
    _Blueprint(
        family="downstream_executor_saturation",
        root_cause_code="inventory_timeout",
        variants=(
            _Variant(
                "Worker executor rejected task: active=64 queue=500 capacity=500",
                "Caller deadline exceeded after waiting 2000 ms for downstream response",
            ),
            _Variant(
                "Bulkhead has no permits and its pending request queue is full",
                "RPC cancelled with DEADLINE_EXCEEDED after upstream saturation",
            ),
            _Variant(
                "Request dispatcher cannot accept work; all 48 threads remain busy",
                "HTTP client read timed out while invoking the downstream component",
            ),
        ),
    ),
    _Blueprint(
        family="cache_connectivity",
        root_cause_code="redis_unavailable",
        variants=(
            _Variant(
                "Cache socket connect failed: target redis.internal:6379 refused connection",
                "Session lookup abandoned after three cache reconnect attempts",
            ),
            _Variant(
                "Unable to open cache channel: connection reset during handshake",
                "Cached profile read failed after retry budget was exhausted",
            ),
            _Variant(
                "All cache cluster endpoints are unreachable from this instance",
                "Token state could not be loaded from the distributed cache",
            ),
        ),
    ),
    _Blueprint(
        family="external_credential_rejection",
        root_cause_code="payment_auth_failure",
        variants=(
            _Variant(
                "Remote provider rejected request with 401: credential is not valid",
                "Charge was marked failed after authorization endpoint rejection",
            ),
            _Variant(
                "Partner API returned unauthorized; configured client secret was declined",
                "Billing command stopped because provider authentication could not complete",
            ),
            _Variant(
                "Upstream signature verification failed for the configured merchant key",
                "Payment attempt moved to failed state after credential validation error",
            ),
        ),
    ),
    _Blueprint(
        family="filesystem_capacity",
        root_cause_code="disk_full",
        variants=(
            _Variant(
                "Write syscall failed with ENOSPC on volume /data",
                "Artifact could not be persisted to the local staging directory",
            ),
            _Variant(
                "Filesystem reports zero writable blocks for application partition",
                "Temporary export file creation failed before upload",
            ),
            _Variant(
                "Storage quota exhausted while extending the service journal",
                "Flush operation failed and the output file was rolled back",
            ),
        ),
    ),
    _Blueprint(
        family="message_broker_backlog",
        root_cause_code="broker_consumer_stall",
        variants=(
            _Variant(
                "Consumer group has made no offset progress for 180 seconds; lag=48210",
                "Event delivery SLA exceeded while records remained unacknowledged",
            ),
            _Variant(
                "Partition assignment is active but committed offset is unchanged",
                "Backlog age crossed the alert threshold for the notification stream",
            ),
            _Variant(
                "Message listener heartbeat continues while processing progress is stalled",
                "Queued domain events were not dispatched before their deadline",
            ),
        ),
    ),
    _Blueprint(
        family="runtime_memory_pressure",
        root_cause_code="jvm_memory_pressure",
        variants=(
            _Variant(
                "GC overhead limit exceeded after repeated full collections",
                "Request worker allocation failed under sustained heap pressure",
            ),
            _Variant(
                "Old generation remains at 99 percent after a full GC cycle",
                "Inbound request cancelled while JVM was paused for collection",
            ),
            _Variant(
                "Java heap allocation failed while expanding result buffer",
                "Service health check missed its deadline during memory reclamation",
            ),
        ),
    ),
    _Blueprint(
        family="name_resolution",
        root_cause_code="dns_resolution_failure",
        variants=(
            _Variant(
                "Name lookup for pricing.internal failed with SERVFAIL",
                "Dependency call aborted because the target hostname had no address",
            ),
            _Variant(
                "Resolver timed out after querying every configured nameserver",
                "HTTP client could not establish route to the named upstream service",
            ),
            _Variant(
                "UnknownHostException raised for the dependency host",
                "Order workflow stopped before connect because endpoint resolution failed",
            ),
        ),
    ),
    _Blueprint(
        family="normal_operation",
        root_cause_code=None,
        variants=(
            _Variant(
                "Request completed successfully in 42 ms",
                "Response status 200 was written to the client",
            ),
            _Variant(
                "First dependency attempt timed out; retry succeeded in 31 ms",
                "Circuit remained closed after transient recovery",
            ),
            _Variant(
                "Connection reset was observed on an idle pooled socket",
                "Replacement connection succeeded and the transaction committed",
            ),
        ),
        is_anomaly=False,
    ),
    _Blueprint(
        family="ood_contract_break",
        root_cause_code=None,
        variants=(
            _Variant(
                "Decoder rejected payload: required field regionCode is absent",
                "Import workflow quarantined the incompatible partner response",
            ),
            _Variant(
                "Response schema version 17 is unsupported by the active deserializer",
                "Adapter could not map the new enum value to the domain model",
            ),
            _Variant(
                "Binary message begins with an unrecognized protocol marker",
                "Consumer routed the undecodable record to the quarantine stream",
            ),
        ),
        is_ood=True,
    ),
)


_FALLBACK_BACKGROUND = (
    "Health probe completed in 4 ms",
    "Configuration refresh found no changes",
    "Metrics batch exported successfully",
    "Scheduled cleanup removed 12 expired entries",
    "Request context initialized for inbound call",
    "Database heartbeat completed",
    "Cache statistics published",
    "Worker poll returned no new tasks",
)

_DISTRACTORS = (
    "Telemetry exporter could not resolve its optional collector; batch dropped",
    "Old idle connection was reset during background validation",
    "Temporary file cleanup skipped a locked artifact",
    "Metrics queue briefly crossed warning threshold and recovered",
)

# Every fault family uses the same neutral service choices.  Family-specific
# names such as ``payment-service`` make a benchmark solvable from topology
# alone, even when the model ignores all log evidence.
_SHARED_ROOT_SERVICES = ("application-worker-a", "application-worker-b")

_DIGITS = re.compile(r"\b\d+\b")


def _opaque(prefix: str, value: str, length: int = 14) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length].upper()
    return f"{prefix}-{digest}"


def _template_id(message: str) -> str:
    # IDs are deliberately opaque.  The normalized text is useful only for
    # making equivalent background messages share a template.
    normalized = _DIGITS.sub("<N>", message.lower())
    return _opaque("T", normalized, 12)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_hash(corpus: BlindCorpus, truth: BlindTruthSidecar) -> str:
    payload = {
        "corpus": corpus.model_dump(mode="json"),
        "truth_sidecar": truth.model_dump(mode="json"),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def verify_manifest(artifacts: BlindDatasetArtifacts) -> bool:
    """Return ``True`` only when the corpus and sidecar match the manifest."""

    return (
        artifacts.manifest.case_count == len(artifacts.corpus.incidents)
        and artifacts.manifest.content_sha256
        == _content_hash(artifacts.corpus, artifacts.truth_sidecar)
    )


def _split_for_group(family: str, variant_index: int, seed: int) -> DatasetSplit:
    """Assign grouped examples while guaranteeing every family in every split.

    The complete wording variant is the grouping unit.  Both service variants
    of the same message stay in one split, otherwise exact evidence text and
    its derived template ID leak from train into test.  A seed-dependent
    rotation assigns the three variants one each to train, dev and test.
    """

    rotation = int(
        hashlib.sha256(f"{seed}|{family}".encode()).hexdigest()[:8], 16
    ) % 3
    bucket = (variant_index + rotation) % 3
    if bucket == 0:
        return "train"
    if bucket == 1:
        return "dev"
    return "test"


def _background_fields(item: str | LogEvent) -> tuple[str, str, str]:
    if isinstance(item, LogEvent):
        # Do not copy metadata, source, line ID, template ID or trace ID: public
        # datasets can encode anomaly labels in those fields.
        level = item.level if item.level in {"INFO", "WARN", "ERROR"} else "INFO"
        return item.message, item.service or "background-worker", level
    return str(item), "background-worker", "INFO"


def _scheduled_perturbation(
    *, seed: int, family: str, occurrence: int, name: str, every: int
) -> bool:
    """Return a deterministic, family-balanced perturbation schedule.

    A global ``case_index % n`` schedule is unsafe here because fault families
    are also selected with a modulus; the two cycles can concentrate every
    missing-evidence example in one family.  A within-family occurrence plus a
    stable family-specific phase gives every family comparable exposure.
    """

    digest = hashlib.sha256(f"{seed}|{family}|{name}".encode("utf-8")).digest()
    phase = int.from_bytes(digest[:4], "big") % every
    return (occurrence + phase) % every == 0


def _base_events(
    blueprint: _Blueprint,
    variant: _Variant,
    root_service: str,
    base_time: datetime,
    trace_id: str,
    case_number: int,
) -> list[_DraftEvent]:
    if not blueprint.is_anomaly:
        # Recovered timeout/reset variants intentionally contain an ERROR
        # followed by explicit success.  A wholly successful message must not
        # receive an ERROR level just because of its global case index.
        direct_lower = variant.direct.lower()
        first_level = (
            "ERROR"
            if "timed out" in direct_lower or "connection reset" in direct_lower
            else "INFO"
        )
        return [
            _DraftEvent(base_time, "api-gateway", "INFO", "Inbound request accepted", trace_id),
            _DraftEvent(base_time + timedelta(milliseconds=80), root_service, first_level, variant.direct, trace_id),
            _DraftEvent(base_time + timedelta(milliseconds=160), root_service, "INFO", variant.support, trace_id),
            _DraftEvent(base_time + timedelta(milliseconds=240), "api-gateway", "INFO", "Request returned HTTP 200", trace_id),
        ]

    direct_level = "CRITICAL" if blueprint.family == "runtime_memory_pressure" else "ERROR"
    return [
        _DraftEvent(base_time, "api-gateway", "INFO", "Inbound request accepted", trace_id),
        _DraftEvent(
            base_time + timedelta(milliseconds=90),
            root_service,
            direct_level,
            variant.direct,
            trace_id,
            True,
            "direct",
        ),
        _DraftEvent(
            base_time + timedelta(milliseconds=180),
            root_service,
            "ERROR",
            variant.support,
            trace_id,
            True,
            "support",
        ),
        _DraftEvent(
            base_time + timedelta(milliseconds=270),
            "workflow-service",
            "WARN",
            "Circuit protection changed state after downstream failures",
            trace_id,
        ),
        _DraftEvent(
            base_time + timedelta(milliseconds=360),
            "api-gateway",
            "ERROR",
            "Upstream request returned HTTP 503",
            trace_id,
        ),
    ]


def _make_case(
    case_index: int,
    seed: int,
    background_pool: Sequence[str | LogEvent],
) -> tuple[Incident, BlindTruthRecord]:
    rng = random.Random(f"{seed}:{case_index}")
    blueprint = _BLUEPRINTS[case_index % len(_BLUEPRINTS)]
    family_occurrence = case_index // len(_BLUEPRINTS)
    variant_index = (case_index // len(_BLUEPRINTS)) % 3
    service_index = (case_index // (len(_BLUEPRINTS) * 3)) % 2
    variant = blueprint.variants[variant_index]
    root_service = _SHARED_ROOT_SERVICES[service_index]
    case_token = _opaque("C", f"{seed}|{case_index}", 12)
    incident_id = _opaque("INC", f"incident|{seed}|{case_index}", 16)
    trace_id = _opaque("TR", f"trace|{seed}|{case_index}", 16)
    # Random-looking deterministic placement prevents timestamp modulo from
    # revealing the family selected by ``case_index % len(_BLUEPRINTS)``.
    time_digest = hashlib.sha256(
        f"{seed}|event-time|{case_index}".encode("utf-8")
    ).digest()
    time_offset_seconds = int.from_bytes(time_digest[:6], "big") % (365 * 24 * 3600)
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=time_offset_seconds
    )
    drafts = _base_events(blueprint, variant, root_service, base_time, trace_id, case_index)
    perturbations: list[str] = []
    trace_corruption_role: Literal["direct", "support", "non_evidence"] | None = None

    noise_count = 2 + rng.randrange(4)
    for noise_index in range(noise_count):
        selected = background_pool[rng.randrange(len(background_pool))]
        message, service, level = _background_fields(selected)
        drafts.append(
            _DraftEvent(
                base_time + timedelta(milliseconds=25 + noise_index * 61),
                service,
                level,
                message,
                trace_id,
            )
        )

    if blueprint.is_anomaly and _scheduled_perturbation(
        seed=seed,
        family=blueprint.family,
        occurrence=family_occurrence,
        name="distractor_fault",
        every=7,
    ):
        drafts.append(
            _DraftEvent(
                base_time + timedelta(milliseconds=315),
                "observability-sidecar",
                "ERROR",
                rng.choice(_DISTRACTORS),
                trace_id,
            )
        )
        perturbations.append("distractor_fault")

    if blueprint.is_anomaly and _scheduled_perturbation(
        seed=seed,
        family=blueprint.family,
        occurrence=family_occurrence,
        name="missing_evidence",
        every=5,
    ):
        candidates = [index for index, item in enumerate(drafts) if item.is_evidence]
        if candidates:
            removed = drafts.pop(rng.choice(candidates))
            perturbations.append("missing_evidence")
            if removed.evidence_role:
                perturbations.append(f"missing_{removed.evidence_role}")

    if _scheduled_perturbation(
        seed=seed,
        family=blueprint.family,
        occurrence=family_occurrence,
        name="duplicate_event",
        every=4,
    ):
        original = rng.choice(drafts)
        # A replayed/duplicated line is not a second independent ground-truth
        # fact.  The Agent may cite it, but evaluation must not require it.
        drafts.append(
            replace(
                original,
                timestamp=original.timestamp + timedelta(milliseconds=2),
                is_evidence=False,
                evidence_role=None,
            )
        )
        perturbations.append("duplicate_event")

    if drafts and _scheduled_perturbation(
        seed=seed,
        family=blueprint.family,
        occurrence=family_occurrence,
        name="wrong_trace",
        every=4,
    ):
        evidence_candidates = [
            (index, item)
            for index, item in enumerate(drafts)
            if item.is_evidence
        ]
        if evidence_candidates:
            role_digest = hashlib.sha256(
                f"{seed}|{blueprint.family}|{family_occurrence}|trace-role".encode(
                    "utf-8"
                )
            ).digest()
            preferred_role = "direct" if role_digest[0] % 2 == 0 else "support"
            preferred = [
                (index, item)
                for index, item in evidence_candidates
                if item.evidence_role == preferred_role
            ]
            target, moved = (preferred or evidence_candidates)[0]
            trace_corruption_role = moved.evidence_role or "non_evidence"
        else:
            target = rng.randrange(len(drafts))
            trace_corruption_role = "non_evidence"
        drafts[target] = replace(
            drafts[target], trace_id=_opaque("TR", f"wrong|{seed}|{case_index}", 16)
        )
        perturbations.append("wrong_trace")

    if _scheduled_perturbation(
        seed=seed,
        family=blueprint.family,
        occurrence=family_occurrence,
        name="out_of_order",
        every=4,
    ):
        rng.shuffle(drafts)
        perturbations.append("out_of_order")

    events: list[LogEvent] = []
    evidence_ids: list[str] = []
    for position, draft in enumerate(drafts):
        line_id = _opaque(
            "L",
            f"{case_token}|{position}|{draft.timestamp.isoformat()}|{draft.message}",
            18,
        )
        event = LogEvent(
            line_id=line_id,
            timestamp=draft.timestamp,
            service=draft.service,
            level=draft.level,
            message=draft.message,
            template_id=_template_id(draft.message),
            event_template=_DIGITS.sub("<N>", draft.message),
            trace_id=draft.trace_id,
            source="blind-mixed-log",
            metadata={},
        )
        events.append(event)
        if draft.is_evidence:
            evidence_ids.append(line_id)

    group_key = f"{blueprint.family}|v{variant_index + 1}"
    group_id = _opaque("G", group_key, 14)
    split = _split_for_group(blueprint.family, variant_index, seed)
    incident = Incident(
        incident_id=incident_id,
        title=f"Incident {case_token[2:]}",
        events=events,
        source="blind-mixed-log",
        metadata={"dataset_version": DATASET_VERSION},
    )
    truth = BlindTruthRecord(
        scenario=blueprint.family,
        fault_family=blueprint.family,
        template_variant=f"v{variant_index + 1}",
        root_service=root_service,
        is_anomaly=blueprint.is_anomaly,
        root_cause_code=blueprint.root_cause_code,
        evidence_ids=evidence_ids,
        split=split,
        group_id=group_id,
        perturbations=sorted(perturbations),
        trace_corruption_role=trace_corruption_role,
    )
    return incident, truth


def build_blind_dataset(
    case_count: int = DEFAULT_CASE_COUNT,
    *,
    seed: int = DEFAULT_SEED,
    background_messages: Sequence[str | LogEvent] | None = None,
) -> BlindDatasetArtifacts:
    """Generate a deterministic benchmark of at least 100 incidents.

    ``background_messages`` may contain raw strings or events loaded from a
    Loghub CSV.  The latter are copied as message/service/level only, so their
    original labels and IDs cannot leak into the benchmark.

    Cases sharing the same fault-family/template-variant group are assigned
    to exactly one split, including their neutral-service variants.  This is
    intentionally stricter than a random line or incident split.
    """

    if case_count < 100:
        raise ValueError(
            "case_count must be at least 100 for a meaningful label-separated benchmark"
        )
    pool: Sequence[str | LogEvent] = background_messages or _FALLBACK_BACKGROUND
    if not pool:
        raise ValueError("background_messages must not be empty")

    incidents: list[Incident] = []
    labels: dict[str, BlindTruthRecord] = {}
    for case_index in range(case_count):
        incident, truth = _make_case(case_index, seed, pool)
        incidents.append(incident)
        labels[incident.incident_id] = truth

    corpus = BlindCorpus(incidents=incidents)
    truth_sidecar = BlindTruthSidecar(labels=labels)
    split_counts = {name: 0 for name in ("train", "dev", "test")}
    scenario_counts: dict[str, int] = {}
    perturbation_counts: dict[str, int] = {}
    trace_corruption_role_counts: dict[str, int] = {}
    for label in labels.values():
        split_counts[label.split] += 1
        scenario_counts[label.scenario] = scenario_counts.get(label.scenario, 0) + 1
        for perturbation in label.perturbations:
            perturbation_counts[perturbation] = perturbation_counts.get(perturbation, 0) + 1
        if label.trace_corruption_role is not None:
            role = label.trace_corruption_role
            trace_corruption_role_counts[role] = (
                trace_corruption_role_counts.get(role, 0) + 1
            )

    manifest = BlindManifest(
        seed=seed,
        case_count=case_count,
        group_count=len({label.group_id for label in labels.values()}),
        background_pool_size=len(pool),
        split_counts=split_counts,
        scenario_counts=dict(sorted(scenario_counts.items())),
        perturbation_counts=dict(sorted(perturbation_counts.items())),
        trace_corruption_role_counts=dict(
            sorted(trace_corruption_role_counts.items())
        ),
        content_sha256=_content_hash(corpus, truth_sidecar),
    )
    return BlindDatasetArtifacts(corpus, truth_sidecar, manifest)


def incidents_for_split(
    artifacts: BlindDatasetArtifacts, split: DatasetSplit
) -> list[Incident]:
    """Return fresh Agent-visible copies for one split, never their labels."""

    selected_ids = {
        incident_id
        for incident_id, label in artifacts.truth_sidecar.labels.items()
        if label.split == split
    }
    return [
        incident.model_copy(deep=True)
        for incident in artifacts.corpus.incidents
        if incident.incident_id in selected_ids
    ]


def export_blind_dataset(
    artifacts: BlindDatasetArtifacts, output_dir: str | Path
) -> dict[str, Path]:
    """Write corpus, truth and manifest to physically separate JSON files."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    corpus_path = target / "incidents.agent.jsonl"
    truth_path = target / "truth.sidecar.json"
    manifest_path = target / "manifest.json"
    corpus_lines = [
        json.dumps(incident.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for incident in artifacts.corpus.incidents
    ]
    corpus_path.write_text("\n".join(corpus_lines) + "\n", encoding="utf-8")
    truth_path.write_text(
        json.dumps(
            artifacts.truth_sidecar.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        json.dumps(
            artifacts.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"corpus": corpus_path, "truth": truth_path, "manifest": manifest_path}


__all__ = [
    "DATASET_VERSION",
    "DEFAULT_CASE_COUNT",
    "DEFAULT_SEED",
    "BlindCorpus",
    "BlindTruthRecord",
    "BlindTruthSidecar",
    "BlindManifest",
    "BlindDatasetArtifacts",
    "build_blind_dataset",
    "incidents_for_split",
    "export_blind_dataset",
    "verify_manifest",
]
