"""Parser for the demo's explicit Spring/Logback text layout.

Supported first-line layout::

    2026-09-21 10:00:00.123 ERROR [order-service] [trace=abc] TEMPLATE_ID - message

Lines that do not start with this header (Java stack frames, ``Caused by``,
etc.) are merged into the previous event.  This is intentionally a documented
adapter, not a claim that one regex handles every company's Logback pattern.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .models import Incident, LogEvent


_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[.,]\d{3})\s+"
    r"(?P<level>[A-Za-z]+)\s+"
    r"\[(?P<service>[^]]+)]\s+"
    r"\[trace=(?P<trace_id>[^]]+)]\s+"
    r"(?P<template_id>[A-Za-z0-9_.-]+)\s+-\s+"
    r"(?P<message>.*)$"
)

# Documented Spring Boot console/file layout, with optional application name.
# Custom layouts and application-group prefixes should use structured JSON.
_BOOT_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]\d{3,9}(?:Z|[+-]\d{2}:?\d{2})?)\s+"
    r"(?P<level>TRACE|DEBUG|INFO|WARN|ERROR|FATAL|CRITICAL|SEVERE)\s+"
    r"\d+\s+---\s+(?P<context>(?:\[[^]]*]\s*){1,3})"
    r"(?P<logger>[A-Za-z0-9_.$-]+)\s*:\s*(?P<message>.*)$"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _match_header(line: str, default_service: str) -> dict[str, str] | None:
    match = _HEADER.match(line)
    if match:
        return match.groupdict()
    match = _BOOT_HEADER.match(line)
    if not match:
        return None
    result = match.groupdict()
    contexts = re.findall(r"\[([^]]*)]", result.pop("context"))
    # Only support [thread], [application][thread], or [application][thread][trace-span].
    trace_id = ""
    if len(contexts) == 3:
        correlation = contexts[-1].strip()
        trace_match = re.fullmatch(r"([0-9a-fA-F]{16,32})[-,][0-9a-fA-F]{16}", correlation)
        if not trace_match:
            raise ValueError("当前文本布局不支持 application-group 或自定义相关ID；请使用 Spring JSON 日志。")
        trace_id = trace_match.group(1)
    result["service"] = contexts[0].strip() if len(contexts) >= 2 else default_service
    result["trace_id"] = trace_id
    result["template_id"] = ""
    return result

_VARIABLES = (
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|MB|GB)?\b"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
)


def mine_template(message: str) -> str:
    """A transparent regex baseline used only when no explicit template exists."""

    first_line = message.splitlines()[0]
    template = first_line
    for pattern in _VARIABLES:
        template = pattern.sub("<*>", template)
    return template


def _fallback_template_id(template: str) -> str:
    digest = hashlib.sha1(template.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"SPR-{digest}"


def parse_spring_log(
    path: str | Path,
    *,
    line_limit: int | None = None,
    max_event_characters: int = 20_000,
) -> Incident:
    log_path = Path(path)
    if line_limit is not None and line_limit <= 0:
        raise ValueError("line_limit must be positive")
    if max_event_characters <= 0:
        raise ValueError("max_event_characters must be positive")
    pending: dict[str, str | int] | None = None
    parsed: list[dict[str, str | int]] = []
    matched_headers = 0

    with log_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if line_limit is not None and line_number > line_limit:
                break
            raw_line = _ANSI.sub("", raw_line.rstrip("\r\n"))
            match = _match_header(raw_line, log_path.stem)
            if match:
                matched_headers += 1
                if pending is not None:
                    parsed.append(pending)
                pending = {**match, "start_line": line_number, "end_line": line_number}
            elif re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", raw_line):
                raise ValueError(f"第 {line_number} 行使用了未支持的日志布局；请按文档导出 Spring JSON。")
            elif pending is not None:
                # An unrecognised timestamped header must not silently become a
                # stack frame of the previous event and hide later errors.
                pending["end_line"] = line_number
                combined = f"{pending['message']}\n{raw_line}"
                if len(combined) <= max_event_characters:
                    pending["message"] = combined
                elif not str(pending["message"]).endswith("[STACK_TRUNCATED]"):
                    pending["message"] = (
                        str(pending["message"])[: max_event_characters - 18]
                        + "\n[STACK_TRUNCATED]"
                    )
            elif raw_line.strip():
                # Preserve an orphan preamble rather than silently dropping data.
                pending = {
                    "timestamp": "1970-01-01 00:00:00.000",
                    "level": "INFO",
                    "service": log_path.stem,
                    "trace_id": "",
                    "template_id": "",
                    "message": raw_line[:max_event_characters],
                    "start_line": line_number,
                    "end_line": line_number,
                }
    if pending is not None:
        parsed.append(pending)
    if parsed and matched_headers == 0:
        raise ValueError("未识别到 Spring 日志头；支持常见 Boot 文本、示例 Logback 布局或单行 JSON。")

    events: list[LogEvent] = []
    for item in parsed:
        message = str(item["message"])
        event_template = mine_template(message)
        explicit_id = str(item["template_id"])
        events.append(
            LogEvent(
                line_id=f"SPRING-{int(item['start_line']):05d}",
                timestamp=_parse_timestamp(str(item["timestamp"])),
                service=str(item["service"]),
                level=str(item["level"]),
                message=message,
                template_id=explicit_id or _fallback_template_id(event_template),
                event_template=event_template,
                trace_id=str(item["trace_id"]) or None,
                source="spring-text",
                metadata={"path": str(log_path.resolve()), "start_line": item["start_line"],
                    "end_line": item["end_line"], "timezone_assumed": not bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", str(item["timestamp"])))},
            )
        )
    return Incident(
        incident_id=f"SPRING-{log_path.stem.upper()}",
        title=f"Spring log {log_path.name}",
        events=events,
        source="spring-text",
        metadata={"path": str(log_path.resolve()), "layout": "spring-text-supported"},
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace(",", ".").replace("Z", "+00:00"))
    # Preserve the previous teaching layout's UTC assumption, explicitly marked.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
