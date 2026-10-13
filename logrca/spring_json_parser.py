"""Read one JSON object per line in Spring ECS or Logstash format."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Incident, LogEvent, normalize_log_level
from .spring_parser import _fallback_template_id, mine_template


def _field(row: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        if path in row and row[path] is not None:
            return row[path]
        value: Any = row
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def parse_spring_json(path: str | Path, *, line_limit: int | None = None, max_event_characters: int = 20000) -> Incident:
    source = Path(path)
    if line_limit is not None and line_limit <= 0:
        raise ValueError("line_limit must be positive")
    if max_event_characters <= 0:
        raise ValueError("max_event_characters must be positive")
    events = []
    with source.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if line_limit is not None and number > line_limit:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                raise ValueError(f"第 {number} 行不是合法的单行 JSON；不支持多行排版的 JSON。") from None
            if not isinstance(row, dict):
                raise ValueError(f"第 {number} 行必须是 JSON 对象。")
            timestamp = _field(row, "@timestamp", "timestamp")
            message = _field(row, "message")
            level = _field(row, "log.level", "level")
            if not all(isinstance(value, str) and value.strip() for value in (timestamp, message, level)):
                raise ValueError(f"第 {number} 行缺少字符串字段 @timestamp/message/log.level（或level）。")
            if normalize_log_level(level) not in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL", "SEVERE"}:
                raise ValueError(f"第 {number} 行日志级别不在支持范围内。")
            try:
                parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"第 {number} 行时间不是 ISO 8601 格式。") from None
            if parsed_time.tzinfo is None:
                raise ValueError(f"第 {number} 行时间缺少时区；请导出带Z或+08:00的时间。")
            service = _field(row, "service.name", "service_name", "spring.application.name", "application_name") or source.stem
            trace_id = _field(row, "trace.id", "traceId", "trace_id")
            stack = _field(row, "error.stack_trace", "stack_trace")
            if not isinstance(service, str) or (trace_id is not None and not isinstance(trace_id, str)):
                raise ValueError(f"第 {number} 行的服务名或 trace_id 类型不正确。")
            if isinstance(stack, str) and stack:
                message += "\n" + stack
            truncated = len(message) > max_event_characters
            if truncated:
                message = message[:max_event_characters - 20] + "\n[STACK_TRUNCATED]"
            template = mine_template(message)
            events.append(LogEvent(
                line_id=f"SPRING-{number:05d}", timestamp=parsed_time, service=service,
                level=level, message=message, trace_id=trace_id or None,
                template_id=_fallback_template_id(template), event_template=template,
                source="spring-json", metadata={"path": str(source.resolve()),
                    "start_line": number, "end_line": number, "message_truncated": truncated},
            ))
    return Incident(incident_id=f"SPRING-{source.stem.upper()}", title=f"Spring JSON {source.name}",
        events=events, source="spring-json", metadata={"path": str(source.resolve()), "layout": "ecs-or-logstash-jsonl"})
