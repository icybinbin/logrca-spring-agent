"""Incident repository and bounded tools exposed to the Agents.

Tools return small dictionaries instead of the entire raw log corpus.  This is
both a token-control strategy and a security boundary: the Agent can retrieve
only the evidence it needs, with deterministic limits.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .diagnostic_signatures import has_known_failure_signal
from .models import Incident, LogEvent
from .security import sanitize_log_text


RUNBOOKS: dict[str, dict[str, list[str] | str]] = {
    "db_pool_exhausted": {
        "title": "数据库连接池耗尽",
        "checks": [
            "核对 Hikari active/idle/pending 指标与数据库 max_connections",
            "检查慢 SQL、长事务以及未关闭连接",
            "按 trace_id 对齐连接池耗尽与上游 5xx 的时间顺序",
        ],
        "remediation": [
            "先限流或降级，避免重试风暴继续占用连接",
            "修复连接泄漏/慢事务后再谨慎调整连接池上限",
            "为 pending connection 和获取连接耗时设置告警",
        ],
    },
    "inventory_timeout": {
        "title": "库存服务线程池饱和导致调用超时",
        "checks": [
            "检查 inventory 线程池 active、queue 与 reject 指标",
            "确认下游延迟是否造成工作线程长期占用",
            "核对超时、重试和熔断参数是否放大流量",
        ],
        "remediation": [
            "启用限流/隔离并停止无上限重试",
            "定位慢任务后调整队列和线程数，而非只扩大线程池",
            "补充线程池拒绝率与队列水位告警",
        ],
    },
    "redis_unavailable": {
        "title": "Redis 连接不可用",
        "checks": [
            "从应用节点探测 redis:6379 的 DNS、网络与 TLS",
            "核对 Redis 实例健康、连接数和最近变更",
            "检查客户端连接池与重试退避配置",
        ],
        "remediation": [
            "恢复 Redis 服务或切换到健康副本",
            "对会话读取设置有界重试和安全降级",
            "增加连接失败率及实例可用性告警",
        ],
    },
    "payment_auth_failure": {
        "title": "支付供应商凭证失效",
        "checks": [
            "核对密钥版本、有效期、环境和供应商账号状态",
            "审计密钥轮换时间与配置发布记录",
            "使用脱敏探针验证供应商鉴权",
        ],
        "remediation": [
            "回滚到有效凭证或完成密钥轮换",
            "禁止在日志中输出密钥原文",
            "增加鉴权失败率和凭证到期预警",
        ],
    },
    "disk_full": {
        "title": "服务节点磁盘空间耗尽",
        "checks": [
            "检查磁盘、inode、挂载点和容器临时卷使用率",
            "定位异常增长的日志、缓存或上传临时文件",
            "核对日志轮转与数据保留策略",
        ],
        "remediation": [
            "清理可安全删除的临时数据或扩容卷",
            "恢复写入前确认文件系统健康",
            "设置磁盘与 inode 分级告警及自动轮转",
        ],
    },
    "dns_resolution_failure": {
        "title": "DNS 名称解析失败",
        "checks": [
            "从故障实例检查目标域名的 DNS 解析结果与 resolver 配置",
            "核对 DNS 服务健康、缓存、搜索域和最近的域名变更",
            "按时间对齐 UnknownHost/NXDOMAIN 与上游连接失败",
        ],
        "remediation": [
            "修复错误域名或恢复 DNS 服务，避免用永久 hosts 记录掩盖问题",
            "为解析失败设置有界重试和合理缓存 TTL",
            "增加 DNS 失败率与关键域名探测告警",
        ],
    },
    "tls_certificate_expired": {
        "title": "TLS 证书失效或握手失败",
        "checks": [
            "检查服务端证书有效期、证书链、SNI 与系统时间",
            "核对信任库和最近的证书轮换/配置发布",
            "使用脱敏握手探针验证具体失败阶段",
        ],
        "remediation": [
            "部署有效证书和完整证书链，并先在非生产探针验证",
            "回滚错误的信任库或证书轮换变更",
            "增加证书到期和 TLS 握手失败率告警",
        ],
    },
    "jvm_memory_pressure": {
        "title": "进程或容器内存耗尽",
        "checks": [
            "检查 heap、RSS、GC、容器 memory limit 和 OOM 事件",
            "核对流量、对象增长、缓存和最近发布变化",
            "确认是内存泄漏、突发峰值还是资源限制过低",
        ],
        "remediation": [
            "先限流或扩容恢复服务，保留 heap dump/诊断信息",
            "定位高占用对象或泄漏后再调整堆与容器限制",
            "增加内存水位、GC 暂停与 OOM 告警",
        ],
    },
    "broker_consumer_stall": {
        "title": "消息队列积压或消费者停滞",
        "checks": [
            "检查 consumer lag、消费速率、分区分配和重平衡记录",
            "核对下游处理延迟、失败重试和死信队列",
            "确认生产速率是否持续高于消费能力",
        ],
        "remediation": [
            "修复停滞消费者或临时扩展消费能力",
            "限制无界重试并隔离持续失败的消息",
            "为 lag 增长率、最长消息年龄和死信量设置告警",
        ],
    },
}


class IncidentStore:
    def __init__(self, incidents: list[Incident] | None = None) -> None:
        self._incidents: dict[str, Incident] = {}
        for incident in incidents or []:
            self.add(incident)

    def add(self, incident: Incident) -> None:
        self._incidents[incident.incident_id] = incident

    def get(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError as exc:
            raise KeyError(f"Unknown incident: {incident_id}") from exc

    @staticmethod
    def _brief(event: LogEvent) -> dict[str, Any]:
        safe_message, security_flags = sanitize_log_text(event.message)
        return {
            "line_id": event.line_id,
            "timestamp": event.timestamp.isoformat(),
            "service": event.service,
            "level": event.level,
            "template_id": event.template_id,
            "message": safe_message,
            "trace_id": event.trace_id,
            "security_flags": security_flags,
        }

    def get_incident_summary(self, incident_id: str) -> dict[str, Any]:
        incident = self.get(incident_id)
        level_counts = Counter(event.level for event in incident.events)
        service_counts = Counter(event.service for event in incident.events)
        return {
            "incident_id": incident.incident_id,
            "title": incident.title,
            "event_count": len(incident.events),
            "time_range": [
                incident.start_time.isoformat() if incident.start_time else None,
                incident.end_time.isoformat() if incident.end_time else None,
            ],
            "levels": dict(level_counts),
            "services": dict(service_counts),
        }

    def search_logs(
        self,
        incident_id: str,
        query: str = "",
        levels: list[str] | None = None,
        service: str | None = None,
        limit: int = 20,
        include_known_failure_signals: bool = False,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query_lower = query.lower().strip()
        level_set = {level.upper() for level in levels or []}
        result: list[dict[str, Any]] = []
        for event in self.get(incident_id).events:
            haystack = f"{event.message} {event.event_template} {event.template_id}".lower()
            if query_lower and query_lower not in haystack:
                continue
            level_matches = not level_set or event.level.upper() in level_set
            semantic_matches = include_known_failure_signals and has_known_failure_signal(
                f"{event.message} {event.event_template}"
            )
            if not level_matches and not semantic_matches:
                continue
            if service and event.service != service:
                continue
            result.append(self._brief(event))
            if len(result) >= limit:
                break
        return result

    def get_timeline(
        self, incident_id: str, line_ids: list[str] | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        events = sorted(self.get(incident_id).events, key=lambda item: item.timestamp)
        if line_ids is not None:
            selected = set(line_ids)
            events = [event for event in events if event.line_id in selected]
        return [self._brief(event) for event in events[:limit]]

    def get_events_by_ids(self, incident_id: str, line_ids: list[str]) -> list[dict[str, Any]]:
        wanted = set(line_ids[:100])
        return [
            self._brief(event)
            for event in self.get(incident_id).events
            if event.line_id in wanted
        ]

    def get_template_stats(self, incident_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        counter = Counter(event.template_id for event in self.get(incident_id).events)
        return [
            {"template_id": template, "count": count}
            for template, count in counter.most_common(limit)
        ]

    def get_runbook(self, root_cause_code: str) -> dict[str, Any]:
        default = {
            "title": "未知根因",
            "checks": ["补充指标、调用链和变更记录后再判断"],
            "remediation": ["避免在证据不足时执行高风险变更"],
        }
        return dict(RUNBOOKS.get(root_cause_code, default))
