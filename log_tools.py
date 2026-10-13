"""Small local tools used by the offline Agent demo."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


DEMO_LOGS: List[Dict[str, Any]] = [
    {
        "line_id": 98,
        "timestamp": "2026-09-20T10:00:01.101Z",
        "service": "gateway",
        "level": "INFO",
        "trace_id": "tr-001",
        "template_id": "GW_REQUEST",
        "message": "request started path=/orders/42",
    },
    {
        "line_id": 99,
        "timestamp": "2026-09-20T10:00:01.110Z",
        "service": "order-service",
        "level": "INFO",
        "trace_id": "tr-001",
        "template_id": "ORDER_START",
        "message": "load order id=42",
    },
    {
        "line_id": 100,
        "timestamp": "2026-09-20T10:00:01.250Z",
        "service": "order-service",
        "level": "ERROR",
        "trace_id": "tr-001",
        "template_id": "DB_POOL_EXHAUSTED",
        "message": "HikariPool-1 - Connection is not available, request timed out after 30000ms",
    },
    {
        "line_id": 101,
        "timestamp": "2026-09-20T10:00:01.260Z",
        "service": "order-service",
        "level": "ERROR",
        "trace_id": "tr-001",
        "template_id": "ORDER_DB_TIMEOUT",
        "message": "query order failed due to datasource timeout",
    },
    {
        "line_id": 102,
        "timestamp": "2026-09-20T10:00:01.271Z",
        "service": "gateway",
        "level": "ERROR",
        "trace_id": "tr-001",
        "template_id": "GW_5XX",
        "message": "request failed status=500 path=/orders/42",
    },
    {
        "line_id": 103,
        "timestamp": "2026-09-20T10:00:02.001Z",
        "service": "inventory-service",
        "level": "INFO",
        "trace_id": "tr-002",
        "template_id": "INVENTORY_HEALTHY",
        "message": "inventory check completed",
    },
]


def search_logs(
    keyword: Optional[str] = None,
    service: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return a bounded set of matching log events.

    In a production system this function would query a log store.  The
    bounded result is intentional: tools should return the smallest useful
    evidence package instead of dumping all raw logs into the model context.
    """

    events = []
    for event in DEMO_LOGS:
        # Filtering belongs in the tool, not in the model prompt.  This keeps
        # the context small and makes the returned evidence reproducible.
        if keyword and keyword.lower() not in event["message"].lower() and keyword.lower() not in event["level"].lower():
            continue
        if service and event["service"] != service:
            continue
        if level and event["level"] != level:
            continue
        events.append(event)

    # A hard limit protects the model from an accidental full-log dump.
    return {"count": len(events), "events": events[:limit]}


def get_service_stats(service: str) -> Dict[str, Any]:
    """Return compact statistics for one service in the incident window."""

    service_events = [event for event in DEMO_LOGS if event["service"] == service]
    error_count = sum(event["level"] == "ERROR" for event in service_events)
    return {
        "service": service,
        "total_events": len(service_events),
        "error_count": error_count,
        "error_rate": round(error_count / max(len(service_events), 1), 3),
    }
