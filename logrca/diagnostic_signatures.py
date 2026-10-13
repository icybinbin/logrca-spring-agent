"""Local semantic signatures used by the offline diagnosis policy.

This module is deliberately *not* a RAG system or a knowledge graph.  It is a
small, reviewable baseline that lets the project run without an API key while
still testing the Agent loop, tool selection, chronology, open-set abstention,
and evidence grounding.  The blind-data generator never imports this module;
its messages and labels are defined independently so evaluation cannot simply
reuse the generator's exact template IDs.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class CauseSignature:
    code: str
    title: str
    direct_patterns: tuple[str, ...]
    support_patterns: tuple[str, ...]
    reasoning: str


@dataclass(frozen=True, slots=True)
class CauseMatch:
    signature: CauseSignature
    score: float
    confidence: float
    direct_ids: tuple[str, ...]
    support_ids: tuple[str, ...]
    first_direct_at: datetime | None


# These expressions describe operational symptoms rather than dataset template
# IDs.  Exact EventIds are intentionally absent: permuting EventIds must not
# change a diagnosis.
CAUSE_SIGNATURES: tuple[CauseSignature, ...] = (
    CauseSignature(
        code="db_pool_exhausted",
        title="数据库连接池耗尽",
        direct_patterns=(
            r"connection (?:is )?not available.*tim(?:e|ed) ?out",
            r"connection (?:lease|checkout|acquisition|wait).*(?:exceed|deadline|tim(?:e|ed) ?out)",
            r"(?:database|jdbc|hikari).*pool.*(?:exhaust|saturat|no idle|timeout)",
            r"datasource.*(?:wait queue|every connection.*in use|no connection)",
            r"could not obtain (?:a )?(?:jdbc|database) connection",
            r"(?:pool|housekeeper).*active\s*[=:]\s*\d+.*idle\s*[=:]\s*0.*(?:pending|waiting)\s*[=:]",
        ),
        support_patterns=(
            r"sqltransientconnectionexception",
            r"unable to acquire.*connection",
            r"(?:database|sql).*connection.*(?:failed|timeout)",
            r"transaction.*(?:no jdbc connection|connection.*available)",
            r"repository operation failed.*connection acquisition",
            r"sql request.*(?:datasource checkout|connection).*(?:cancel|timed out)",
        ),
        reasoning="最早出现连接池无可用连接或连接获取超时，随后数据库操作失败并向上游传播。",
    ),
    CauseSignature(
        code="inventory_timeout",
        title="工作线程池饱和导致下游超时",
        direct_patterns=(
            r"task rejected.*threadpoolexecutor",
            r"rejectedexecutionexception",
            r"thread\s*pool.*(?:saturat|exhaust|queue.*full|reject)",
            r"executor.*active\s*[=:].*queued\s*[=:].*(?:reject|capacity)",
            r"(?:worker )?executor.*(?:reject|queue.*(?:full|capacity))",
            r"bulkhead.*(?:no permits|queue.*full)",
            r"dispatcher.*(?:cannot accept|threads?.*busy)",
        ),
        support_patterns=(
            r"(?:request|read|upstream|client).*tim(?:e|ed) ?out",
            r"(?:caller|rpc).*deadline[_ ]exceeded",
        ),
        reasoning="执行器首先拒绝或积压任务，随后调用方超时或熔断，符合线程池饱和的传播顺序。",
    ),
    CauseSignature(
        code="redis_unavailable",
        title="Redis/缓存服务连接不可用",
        direct_patterns=(
            r"redis.*(?:connection refused|unavailable|cannot connect|connection reset)",
            r"(?:lettuce|jedis).*connectionexception",
            r"connection refused.*(?:redis|:6379)",
            r"(?:cache|redis).*(?:socket|connect|channel|endpoint|cluster).*(?:refused|reset|unreachable|failed)",
            r"(?:cache|redis).*(?:unreachable|connection reset)",
        ),
        support_patterns=(
            r"(?:session|cache).*(?:lookup|read|operation).*(?:failed|timeout|abandoned)",
            r"cache.*retry.*(?:failed|exhaust)",
            r"(?:token state|cached profile).*(?:could not be loaded|failed)",
        ),
        reasoning="缓存连接错误早于会话或缓存读取失败，后者属于业务层级联症状。",
    ),
    CauseSignature(
        code="payment_auth_failure",
        title="外部服务凭证或鉴权失败",
        direct_patterns=(
            r"(?:401|403).*(?:invalid|expired|unauthori[sz]ed|api[ _-]?key|credential)",
            r"(?:invalid|expired).*(?:api[ _-]?key|credential|token)",
            r"authentication.*(?:failed|rejected).*(?:provider|upstream)",
            r"(?:provider|partner api).*(?:401|unauthori[sz]ed).*(?:credential|secret|key)?",
            r"signature verification failed.*(?:merchant|client).*(?:key|secret)",
        ),
        support_patterns=(
            r"(?:payment|charge|billing).*(?:failed|rejected|stopped)",
            r"provider.*(?:rejected|denied)",
        ),
        reasoning="外部服务先明确返回鉴权/凭证错误，随后业务请求被标记失败。",
    ),
    CauseSignature(
        code="disk_full",
        title="磁盘或文件系统容量耗尽",
        direct_patterns=(
            r"no space left on device",
            r"\benospc\b",
            r"(?:disk|filesystem|volume).*(?:full|capacity exhausted)",
            r"(?:quota|inode).*(?:exceeded|exhausted)",
            r"filesystem.*(?:zero|no) writable blocks",
        ),
        support_patterns=(
            r"(?:write|persist|flush).*(?:failed|unable|error)",
            r"unable to persist",
            r"(?:could not|unable to) be persisted",
            r"(?:file creation|create.*file).*(?:failed|error)",
            r"read-only file system",
        ),
        reasoning="底层容量或 inode 错误首先出现，随后持久化、写文件或接口请求失败。",
    ),
    CauseSignature(
        code="dns_resolution_failure",
        title="DNS 名称解析失败",
        direct_patterns=(
            r"unknownhostexception",
            r"name or service not known",
            r"temporary failure in name resolution",
            r"(?:dns|hostname).*(?:resolution|lookup).*(?:failed|timeout|nxdomain)",
            r"no such host",
            r"name lookup.*(?:failed|servfail|nxdomain)",
            r"resolver.*(?:timed out|timeout|nameserver)",
        ),
        support_patterns=(
            r"upstream.*(?:unreachable|connect.*failed)",
            r"connection.*(?:failed|timeout).*(?:host|endpoint)",
            r"(?:dependency|http client|workflow).*(?:hostname|named upstream|endpoint resolution).*(?:no address|could not|failed|stopped|aborted)",
        ),
        reasoning="名称解析错误早于连接或上游不可达错误，说明请求尚未到达目标服务。",
    ),
    CauseSignature(
        code="tls_certificate_expired",
        title="TLS 证书失效或握手失败",
        direct_patterns=(
            r"certificate.*(?:expired|not yet valid|unknown)",
            r"sslhandshakeexception",
            r"certpathvalidatorexception",
            r"unable to find valid certification path",
        ),
        support_patterns=(
            r"(?:tls|ssl).*handshake.*(?:failed|error)",
            r"peer.*(?:unverified|closed connection)",
        ),
        reasoning="证书校验或 TLS 握手错误首先出现，随后安全连接建立失败。",
    ),
    CauseSignature(
        code="jvm_memory_pressure",
        title="进程或容器内存耗尽",
        direct_patterns=(
            r"outofmemoryerror",
            r"java heap space",
            r"gc overhead limit exceeded",
            r"cannot allocate memory",
            r"oomkill|oom killed|killed process.*out of memory",
            r"old generation.*(?:9\d|100) percent.*full gc",
            r"java heap allocation failed",
        ),
        support_patterns=(
            r"allocation.*failed",
            r"container.*(?:oom|memory limit)",
            r"process.*(?:terminated|restarted).*(?:memory|exit 137)",
            r"(?:request|health check).*(?:cancelled|missed.*deadline).*(?:jvm|memory|collection)",
        ),
        reasoning="内存分配或 OOM 错误先出现，随后进程退出、重启或请求失败。",
    ),
    CauseSignature(
        code="broker_consumer_stall",
        title="消息队列积压或消费者停滞",
        direct_patterns=(
            r"(?:consumer|partition).*(?:lag|backlog).*(?:high|exceed|growing)",
            r"records-lag-max",
            r"queue.*(?:full|capacity|backlog)",
            r"consumer.*(?:stalled|not polling|rebalance loop)",
            r"consumer.*(?:no offset progress|offset.*unchanged)",
            r"partition.*committed offset.*unchanged",
            r"message listener.*progress.*stalled",
        ),
        support_patterns=(
            r"message.*(?:delivery|processing).*(?:delayed|timeout)",
            r"event.*(?:late|not processed)",
            r"event delivery.*(?:sla|deadline).*(?:exceed|miss)",
            r"backlog age.*(?:threshold|high|exceed)",
            r"queued.*events.*(?:not dispatched|delayed)",
        ),
        reasoning="消费延迟或队列容量信号先异常，随后消息处理超时或业务数据延迟。",
    ),
)


_COMPILED: dict[str, tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]] = {
    signature.code: (
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in signature.direct_patterns),
        tuple(re.compile(pattern, re.IGNORECASE) for pattern in signature.support_patterns),
    )
    for signature in CAUSE_SIGNATURES
}

_SYMPTOM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:gateway|upstream).*(?:500|502|503|504|5xx)",
        r"client retry|retry scheduled|retry attempt",
        r"circuit breaker.*open",
        r"request (?:failed|aborted)",
        r"service unavailable",
    )
)


def _row_text(row: dict[str, Any]) -> str:
    # Template IDs are deliberately excluded to make the baseline invariant to
    # opaque ID remapping.  Event templates/messages are observable log text.
    return f"{row.get('message', '')} {row.get('event_template', '')}".strip()


def _matches_any(text: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def classify_evidence(code: str, row: dict[str, Any]) -> str | None:
    """Classify one row for one candidate without consulting dataset labels."""

    compiled = _COMPILED.get(code)
    if compiled is None:
        return None
    text = _row_text(row)
    direct, support = compiled
    if _matches_any(text, direct):
        return "direct"
    if _matches_any(text, support):
        return "support"
    if _matches_any(text, _SYMPTOM_PATTERNS):
        return "symptom"
    return None


def has_known_failure_signal(text: str) -> bool:
    """Return whether text contains a direct high-signal operational failure."""

    return any(
        _matches_any(text, _COMPILED[signature.code][0])
        for signature in CAUSE_SIGNATURES
    )


def is_generic_symptom(row: dict[str, Any]) -> bool:
    """Identify propagation symptoms without assigning a root-cause class."""

    return _matches_any(_row_text(row), _SYMPTOM_PATTERNS)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def rank_causes(rows: list[dict[str, Any]]) -> list[CauseMatch]:
    """Rank known causes using semantics, chronology, and evidence diversity.

    A cause is returned only when at least one direct signature is present.  A
    support-only match is not enough to manufacture a root cause, which is the
    key open-set/abstention behavior exercised by the P0 benchmark.
    """

    matches: list[CauseMatch] = []
    services_by_id = {str(row.get("line_id")): str(row.get("service", "")) for row in rows}
    for signature in CAUSE_SIGNATURES:
        direct_ids: list[str] = []
        support_ids: list[str] = []
        direct_times: list[datetime] = []
        for row in rows:
            kind = classify_evidence(signature.code, row)
            line_id = str(row.get("line_id", ""))
            if not line_id:
                continue
            if kind == "direct":
                direct_ids.append(line_id)
                timestamp = _parse_timestamp(row.get("timestamp"))
                if timestamp is not None:
                    direct_times.append(timestamp)
            elif kind == "support":
                support_ids.append(line_id)
        if not direct_ids:
            continue

        evidence_services = {
            services_by_id[line_id]
            for line_id in (*direct_ids, *support_ids)
            if services_by_id.get(line_id)
        }
        # Repeated direct observations are useful but have diminishing returns.
        raw_score = (
            3.0
            + 1.35 * math.log2(1 + len(direct_ids))
            + 0.7 * math.log2(1 + len(support_ids))
            + (0.25 if len(evidence_services) > 1 else 0.0)
        )
        confidence = min(
            0.96,
            0.48
            + min(len(direct_ids), 3) * 0.12
            + min(len(support_ids), 3) * 0.055
            + (0.035 if len(evidence_services) > 1 else 0.0),
        )
        matches.append(
            CauseMatch(
                signature=signature,
                score=round(raw_score, 4),
                confidence=round(confidence, 4),
                direct_ids=tuple(dict.fromkeys(direct_ids)),
                support_ids=tuple(dict.fromkeys(support_ids)),
                first_direct_at=min(direct_times) if direct_times else None,
            )
        )

    # Stronger evidence wins; ties prefer the cause whose direct evidence was
    # observed first instead of relying on catalog insertion order.
    return sorted(
        matches,
        key=lambda item: (
            -item.score,
            item.first_direct_at.timestamp() if item.first_direct_at is not None else math.inf,
            item.signature.code,
        ),
    )


def signature_for(code: str) -> CauseSignature | None:
    return next((item for item in CAUSE_SIGNATURES if item.code == code), None)
