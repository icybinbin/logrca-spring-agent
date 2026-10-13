"""Small runtime guardrails for treating log text as untrusted data."""

from __future__ import annotations

import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
)

_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"),
    re.compile(r"(?i)system\s+prompt"),
    re.compile(r"(?i)调用工具|执行命令|删除.*数据"),
)


def sanitize_log_text(text: str) -> tuple[str, list[str]]:
    """Redact common secret shapes and flag instruction-like log payloads.

    This is intentionally a baseline, not a complete DLP system.  Production
    deployments should perform tenant-specific redaction before log storage.
    """

    sanitized = text
    flags: list[str] = []
    for pattern in _SECRET_PATTERNS:
        if pattern.search(sanitized):
            flags.append("secret_redacted")
            if "Bearer" in pattern.pattern:
                sanitized = pattern.sub("Bearer [REDACTED]", sanitized)
            else:
                sanitized = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", sanitized)
    if any(pattern.search(sanitized) for pattern in _INJECTION_PATTERNS):
        flags.append("instruction_like_content")
    return sanitized, sorted(set(flags))
