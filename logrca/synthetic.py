"""Small deterministic Spring-style incidents with evaluation labels.

Public Loghub data is useful for ingestion and anomaly-detection experiments,
but most datasets do not contain a complete business-service causal chain.  We
therefore use transparent synthetic incidents for root-cause evaluation and
keep the ground truth separate from the events seen by the Agent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import GroundTruth, Incident, LogEvent, SyntheticCase


SCENARIOS = (
    "normal",
    "db_pool_exhausted",
    "inventory_timeout",
    "redis_unavailable",
    "payment_auth_failure",
    "disk_full",
)

_CASE_IDS = {
    "normal": "001",
    "db_pool_exhausted": "002",
    "inventory_timeout": "003",
    "redis_unavailable": "004",
    "payment_auth_failure": "005",
    "disk_full": "006",
}

_BASE_TIME = datetime(2026, 9, 21, 10, 0, 0, tzinfo=timezone.utc)


def _event(
    scenario: str,
    number: int,
    service: str,
    level: str,
    template_id: str,
    message: str,
    trace_id: str,
) -> LogEvent:
    return LogEvent(
        line_id=f"EVT-{_CASE_IDS[scenario]}-{number:03d}",
        timestamp=_BASE_TIME + timedelta(milliseconds=number * 170),
        service=service,
        level=level,
        message=message,
        template_id=template_id,
        event_template=message,
        trace_id=trace_id,
        source="synthetic-spring",
    )


def _make_case(
    scenario: str,
    rows: Iterable[tuple[str, str, str, str]],
    *,
    is_anomaly: bool,
    root_cause_code: str | None,
    evidence_numbers: list[int],
) -> SyntheticCase:
    trace_id = f"trace-syn-{_CASE_IDS[scenario]}-001"
    events = [
        _event(scenario, index, service, level, template, message, trace_id)
        for index, (service, level, template, message) in enumerate(rows, start=1)
    ]
    incident = Incident(
        incident_id=f"INC-SYN-{_CASE_IDS[scenario]}",
        title=f"Synthetic incident {_CASE_IDS[scenario]}",
        events=events,
        metadata={"generator": "spring-demo-v1"},
    )
    prefix = f"EVT-{_CASE_IDS[scenario]}"
    truth = GroundTruth(
        scenario=scenario,
        is_anomaly=is_anomaly,
        root_cause_code=root_cause_code,
        evidence_ids=[f"{prefix}-{number:03d}" for number in evidence_numbers],
    )
    return SyntheticCase(incident=incident, truth=truth)


def build_scenario(name: str) -> SyntheticCase:
    """Return a fresh case so callers cannot mutate a global fixture."""

    if name == "normal":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "GET /api/orders request accepted"),
                ("order-service", "INFO", "ORDER_QUERY", "Querying order id=9281"),
                ("inventory-service", "INFO", "INVENTORY_OK", "Inventory lookup completed in 18ms"),
                ("order-service", "INFO", "ORDER_OK", "Order query completed in 31ms"),
                ("gateway", "INFO", "HTTP_200", "GET /api/orders returned 200 in 37ms"),
            ],
            is_anomaly=False,
            root_cause_code=None,
            evidence_numbers=[],
        )
    if name == "db_pool_exhausted":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "POST /api/orders request accepted"),
                ("order-service", "INFO", "ORDER_CREATE", "Creating order id=7319"),
                ("order-service", "ERROR", "DB_POOL_EXHAUSTED", "HikariPool-1 - Connection is not available, request timed out after 30000ms"),
                ("order-service", "ERROR", "DB_CONNECTION_FAILED", "SQLTransientConnectionException while saving order 7319"),
                ("gateway", "ERROR", "GATEWAY_5XX", "order-service returned HTTP 503 for POST /api/orders"),
                ("gateway", "WARN", "CLIENT_RETRY", "Client retry scheduled after upstream failure"),
            ],
            is_anomaly=True,
            root_cause_code="db_pool_exhausted",
            evidence_numbers=[3, 4],
        )
    if name == "inventory_timeout":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "POST /api/checkout request accepted"),
                ("checkout-service", "INFO", "CHECKOUT_START", "Checkout started for cart 882"),
                ("inventory-service", "ERROR", "INVENTORY_THREAD_POOL_SATURATED", "Task rejected from ThreadPoolExecutor: active=64 queued=500"),
                ("checkout-service", "ERROR", "HTTP_CLIENT_TIMEOUT", "inventory-service request timed out after 2000ms"),
                ("checkout-service", "WARN", "CIRCUIT_OPEN", "Circuit breaker inventory transitioned to OPEN"),
                ("gateway", "ERROR", "GATEWAY_5XX", "checkout-service returned HTTP 503"),
            ],
            is_anomaly=True,
            root_cause_code="inventory_timeout",
            evidence_numbers=[3, 4],
        )
    if name == "redis_unavailable":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "GET /api/profile request accepted"),
                ("user-service", "INFO", "PROFILE_QUERY", "Loading profile for user 42"),
                ("user-service", "ERROR", "REDIS_CONNECTION_REFUSED", "RedisConnectionException: Connection refused redis:6379"),
                ("user-service", "ERROR", "SESSION_LOOKUP_FAILED", "Session lookup failed after 3 attempts"),
                ("gateway", "ERROR", "GATEWAY_5XX", "user-service returned HTTP 500"),
            ],
            is_anomaly=True,
            root_cause_code="redis_unavailable",
            evidence_numbers=[3, 4],
        )
    if name == "payment_auth_failure":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "POST /api/payments request accepted"),
                ("payment-service", "INFO", "PAYMENT_START", "Payment attempt p-551 started"),
                ("payment-service", "ERROR", "PAYMENT_PROVIDER_401", "Provider returned 401 invalid API key"),
                ("payment-service", "ERROR", "PAYMENT_FAILED", "Payment p-551 marked failed after provider rejection"),
                ("gateway", "ERROR", "GATEWAY_5XX", "payment-service returned HTTP 502"),
            ],
            is_anomaly=True,
            root_cause_code="payment_auth_failure",
            evidence_numbers=[3, 4],
        )
    if name == "disk_full":
        return _make_case(
            name,
            [
                ("gateway", "INFO", "HTTP_REQUEST", "POST /api/uploads request accepted"),
                ("file-service", "INFO", "UPLOAD_START", "Writing upload 991 to local staging"),
                ("file-service", "ERROR", "NO_SPACE_LEFT", "java.io.IOException: No space left on device"),
                ("file-service", "ERROR", "FILE_WRITE_FAILED", "Unable to persist upload 991 to /data/staging"),
                ("gateway", "ERROR", "GATEWAY_5XX", "file-service returned HTTP 500"),
            ],
            is_anomaly=True,
            root_cause_code="disk_full",
            evidence_numbers=[3, 4],
        )
    raise ValueError(f"Unknown scenario {name!r}. Choose from: {', '.join(SCENARIOS)}")


def all_scenarios() -> list[SyntheticCase]:
    return [build_scenario(name) for name in SCENARIOS]
