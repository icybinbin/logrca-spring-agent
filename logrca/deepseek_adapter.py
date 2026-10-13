"""DeepSeek tool-calling policy on the existing framework-independent runtime.

No SDK, MCP server or rule-based root selection is needed on this path.
The manager owns the workflow; each model specialist chooses its own tools.
Credentials remain in DEEPSEEK_API_KEY. Official protocol references:
https://api-docs.deepseek.com/zh-cn/guides/tool_calls/
https://api-docs.deepseek.com/zh-cn/guides/json_mode/
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .agents import EVIDENCE_AUDITOR_INSTRUCTIONS, HYPOTHESIS_AGENT_INSTRUCTIONS
from .detector import AnomalyDetector
from .models import AuditResult, DiagnosisReport, HypothesisBundle, Incident, Severity
from .openai_adapter import compose_audited_report
from .runtime import AgentRuntime, AgentState, FinalAction, ToolAction, ToolRegistry, TraceRecorder
from .security import sanitize_log_text
from .store import IncidentStore, RUNBOOKS


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe(value: str, key: str = "") -> str:
    if key:
        value = value.replace(key, "[REDACTED]")
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    return sanitize_log_text(value)[0]


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return _safe(value)
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe_value(item) for key, item in value.items()}
    return value


class DeepSeekError(RuntimeError):
    """A bounded, redacted provider/protocol failure; never silently falls back."""


class ModelBudgetError(DeepSeekError):
    pass


@dataclass(frozen=True)
class DeepSeekConfig:
    model: str = "deepseek-flash"
    max_requests: int = 12  # shared by hypothesis and auditor for ONE incident
    max_requests_per_agent: int = 6
    request_timeout: float = 30.0
    incident_timeout: float = 180.0
    max_output_tokens: int = 1800
    max_total_tokens: int = 20000  # stop further calls after measured usage reaches this
    max_context_characters: int = 60000
    max_tools_per_response: int = 4

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must be nonempty")
        for name in self.__dataclass_fields__:
            if name != "model" and getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


class DeepSeekTransport:
    """The same urllib transport that worked in the user's connection check."""

    def complete(self, body: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise DeepSeekError("未设置 DEEPSEEK_API_KEY；请在运行命令的 PowerShell 中设置。")
        request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=_dump(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            # No automatic retries: do not hide repeated paid requests.
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read(2_000_001).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response must be a JSON object")
            return payload
        except urllib.error.HTTPError as exc:
            detail = _safe(exc.read(4096).decode("utf-8", errors="replace"), key)[:1000]
            raise DeepSeekError(f"DeepSeek HTTP {exc.code}: {detail}") from None
        except Exception as exc:
            raise DeepSeekError(f"{type(exc).__name__}: {_safe(str(exc), key)[:1000]}") from None


class RequestSession:
    """One incident's shared budget, measured usage and provider Trace."""

    def __init__(self, config: DeepSeekConfig, transport: DeepSeekTransport, trace: TraceRecorder):
        self.config, self.transport, self.trace = config, transport, trace
        self.started = time.perf_counter()
        self.telemetry: dict[str, Any] = {
            "backend": "deepseek", "requested_model": config.model, "returned_models": [],
            "requests": 0, "completed_requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0, "cache_hit_tokens": 0,
            "cache_miss_tokens": 0, "usage_missing_requests": 0, "api_latency_ms": 0.0,
        }

    def request(self, actor: str, step: int, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        cfg, stats = self.config, self.telemetry
        remaining = cfg.incident_timeout - (time.perf_counter() - self.started)
        if stats["requests"] >= cfg.max_requests or remaining <= 0:
            raise ModelBudgetError("达到事故的模型请求次数或执行时间上限。")
        if stats["total_tokens"] >= cfg.max_total_tokens:
            raise ModelBudgetError("已报告的 Token 用量达到上限，停止追加模型调用。")
        if len(_dump(messages)) > cfg.max_context_characters:
            raise ModelBudgetError("上下文字符数超过上限；请缩小单次日志范围。")
        body = {
            "model": cfg.model, "messages": messages, "tools": tools,
            "tool_choice": "auto", "max_tokens": min(cfg.max_output_tokens, cfg.max_total_tokens - stats["total_tokens"]),
            "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"},
        }
        stats["requests"] += 1
        self.trace.record(actor, "model_request", step, {
            "model": cfg.model, "request_number": stats["requests"],
            "message_count": len(messages), "context_characters": len(_dump(messages)),
            "max_output_tokens": cfg.max_output_tokens,
        })
        started = time.perf_counter()
        try:
            response = self.transport.complete(body, timeout=min(cfg.request_timeout, remaining))
        except Exception as exc:
            self.trace.record(actor, "model_error", step, {"error": type(exc).__name__, "message": _safe(str(exc))[:1000]})
            raise
        finally:
            stats["api_latency_ms"] += round((time.perf_counter() - started) * 1000, 3)
        stats["completed_requests"] += 1
        usage = response.get("usage") or {}
        if not usage:
            stats["usage_missing_requests"] += 1
        for source, target in (
            ("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"), ("prompt_cache_hit_tokens", "cache_hit_tokens"),
            ("prompt_cache_miss_tokens", "cache_miss_tokens"),
        ):
            value = usage.get(source, 0)
            if isinstance(value, int) and value >= 0:
                stats[target] += value
        if "total_tokens" not in usage:
            stats["total_tokens"] = stats["prompt_tokens"] + stats["completion_tokens"]
        returned_model = response.get("model")
        if isinstance(returned_model, str) and returned_model not in stats["returned_models"]:
            stats["returned_models"].append(returned_model)
        self.trace.record(actor, "model_response", step, {
            "returned_model": returned_model, "usage": usage,
            "request_number": stats["requests"],
        }, (time.perf_counter() - started) * 1000)
        return response


class ToolParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchParameters(ToolParameters):
    query: str = Field(default="", max_length=200)
    levels: list[Literal["INFO", "DEBUG", "TRACE", "WARN", "ERROR", "CRITICAL", "FATAL", "SEVERE"]] | None = Field(default=None, max_length=8)
    service: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, ge=1, le=30)


class TimelineParameters(ToolParameters):
    line_ids: list[str] | None = Field(default=None, max_length=30)
    limit: int = Field(default=20, ge=1, le=30)


class EvidenceParameters(ToolParameters):
    line_ids: list[str] = Field(min_length=1, max_length=30)


class RunbookParameters(ToolParameters):
    root_cause_code: str = Field(max_length=100)


def _bounded_result(result: Any) -> Any:
    """Keep complete IDs, bounded excerpts and explicit truncation markers."""
    result = _safe_value(result)
    if isinstance(result, list):
        bounded = []
        for row in result:
            row = {key: value for key, value in row.items() if key != "template_id"}
            message = row.get("message", "")
            if len(message) > 1000:
                row["message"] = message[:1000] + " [日志摘录已截断]"
            if len(_dump(bounded + [row])) > 12000:
                bounded.append({"truncated": True, "message": "工具返回达到字符上限，可按 line_id 分批查询。"})
                break
            bounded.append(row)
        return bounded
    return result


def build_model_tools(store: IncidentStore, incident: Incident) -> tuple[ToolRegistry, dict[str, dict[str, Any]]]:
    """Close over the incident ID: models cannot switch to another incident."""
    registry, schemas = ToolRegistry(), {}
    definitions = {
        "get_incident_summary": (ToolParameters, "查看当前事故的时间、服务与日志级别摘要。", store.get_incident_summary),
        "search_logs": (SearchParameters, "检索当前事故的脱敏日志；不传 levels 可以查看所有级别，最多30条。", store.search_logs),
        "get_timeline": (TimelineParameters, "按时间排序查询当前事故日志，可按 line_ids 缩小范围。", store.get_timeline),
        "get_events_by_ids": (EvidenceParameters, "读取当前事故指定 line_id 的原始日志摘录以核验证据。", store.get_events_by_ids),
        "get_runbook": (RunbookParameters, "按根因 code 读取人工整理的本地排查建议；只读，不执行修复。", store.get_runbook),
    }
    valid_ids = {event.line_id for event in incident.events}
    def make_execute(parameters, function, name):
        def execute(**arguments):
            try:
                parsed = parameters.model_validate(arguments)
            except ValidationError as exc:
                errors = [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]
                raise ValueError(f"工具参数不符合 schema: {_dump(errors)}") from None
            kwargs = parsed.model_dump()
            if "line_ids" in kwargs and kwargs["line_ids"] is not None:
                if any(item not in valid_ids for item in kwargs["line_ids"]):
                    raise ValueError("line_ids 包含当前事故不存在的引用。")
            if name == "get_runbook":
                if kwargs["root_cause_code"] not in {*RUNBOOKS, "unknown"}:
                    raise ValueError("root_cause_code 不属于当前支持的分类。")
                return _bounded_result(function(**kwargs))
            return _bounded_result(function(incident.incident_id, **kwargs))
        return execute

    for name, (parameters, description, function) in definitions.items():
        registry.register(name, make_execute(parameters, function, name))
        schemas[name] = {"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters.model_json_schema(),
        }}
    return registry, schemas


class DeepSeekPolicy:
    """Translate real tool_calls into Runtime actions; replay results by call ID.

    A deque handles multiple calls in one response. All their results must be
    appended BEFORE the next model request. Runtime executes one action/step.
    """

    def __init__(self, session: RequestSession, schemas: dict[str, dict[str, Any]], allowed: set[str], output_type: type[BaseModel]):
        self.session, self.allowed, self.output_type = session, allowed, output_type
        self.tools = [schemas[name] for name in sorted(allowed)]
        self.messages: list[dict[str, Any]] = []
        self.pending: deque[dict[str, Any]] = deque()
        self.waiting_id: str | None = None
        self.observed = 0
        self.seen_ids: set[str] = set()
        self.request_count = 0
        self.repairs = 0

    def decide(self, state: AgentState) -> ToolAction | FinalAction:
        if not self.messages:
            instructions = state.task["instructions"]
            contract = self.output_type.model_json_schema()
            catalog = {code: item["title"] for code, item in RUNBOOKS.items()}
            example = {"incident_id": state.task["incident_id"], "hypotheses": [{
                "code": "unknown", "title": "证据不足", "score": 0.2,
                "reasoning": "尚缺少直接原因的日志", "evidence": [],
            }]} if self.output_type is HypothesisBundle else {
                "accepted": [], "rejected": [], "confidence_multiplier": 0.3,
                "notes": ["请基于工具返回的原始摘录核验引用"],
            }
            self.messages = [
                {"role": "system", "content": instructions + "\n最终返回 JSON 对象，严格符合 schema；不要 Markdown。"
                 "至多3个候选，每个候选至多20条证据。先调用工具看日志，再输出。"
                 "只引用亲自从工具结果中读取的 line_id，template_id 不作为根因证据。"
                 "根因只能选择以下 code 或 unknown。连接获取超时不证明存在连接泄漏或慢SQL，"
                 "这类更深原因应列为待验证。未知/只有5xx症状时选择 unknown。"
                 "审核时不得把候选的 symptom 提升为 direct；必须用工具重新读取候选引用。"
                 + "\n分类: " + _dump(catalog) + "\n输出 schema: " + _dump(contract)
                 + "\nJSON 格式样例（只示意格式，实际结论按日志判断）: " + _dump(example)},
                {"role": "user", "content": _dump({key: value for key, value in state.task.items() if key != "instructions"})},
            ]
        if len(state.observations) > self.observed:
            observation = state.observations[-1]
            if self.waiting_id is None:
                raise DeepSeekError("没有待回传的 tool_call_id。")
            result = observation["result"]
            self.messages.append({"role": "tool", "tool_call_id": self.waiting_id, "content": _dump(result)})
            if isinstance(result, list):
                self.seen_ids.update(row["line_id"] for row in result if isinstance(row, dict) and "line_id" in row)
            self.observed = len(state.observations)
            self.waiting_id = None
        while not self.pending:
            if self.request_count >= self.session.config.max_requests_per_agent:
                raise ModelBudgetError(f"{state.agent_name} 达到单专家请求上限。")
            self.request_count += 1
            response = self.session.request(state.agent_name, state.step, self.messages, self.tools)
            try:
                choice = response["choices"][0]
                message = choice["message"]
            except (KeyError, IndexError, TypeError):
                raise DeepSeekError("模型响应缺少 choices/message。") from None
            if choice.get("finish_reason") not in {"stop", "tool_calls"}:
                raise DeepSeekError(f"模型响应未正常完成: {choice.get('finish_reason')}；未采纳截断的输出。")
            calls = message.get("tool_calls") or []
            assistant = {"role": "assistant", "content": message.get("content")}
            if calls:
                if len(calls) > self.session.config.max_tools_per_response:
                    raise ModelBudgetError("单轮模型请求的工具数超过上限。")
                ids = [call.get("id") for call in calls]
                if any(not isinstance(call_id, str) or not call_id for call_id in ids) or len(set(ids)) != len(ids):
                    raise DeepSeekError("工具调用缺少唯一的 tool_call_id。")
                for call in calls:
                    function = call.get("function") or {}
                    if call.get("type") != "function" or not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), str):
                        raise DeepSeekError("工具调用的 function/arguments 格式不合法。")
                assistant["tool_calls"] = calls
                self.messages.append(assistant)
                self.pending.extend(calls)
                break
            self.messages.append(assistant)
            try:
                output = self.output_type.model_validate_json(message.get("content") or "", strict=True)
                if isinstance(output, HypothesisBundle):
                    if output.incident_id != state.task["incident_id"] or not 1 <= len(output.hypotheses) <= 3:
                        raise ValueError("incident_id 必须匹配；候选数量必须为1至3。")
                    if any(item.code not in {*RUNBOOKS, "unknown"} or len(item.evidence) > 20 for item in output.hypotheses):
                        raise ValueError("候选 code 未知或证据超过20条。")
                if not self.seen_ids:
                    raise ValueError("必须先调用工具读取日志；不能凭空生成诊断或审核。")
            except (ValidationError, ValueError):
                if self.repairs >= 1:
                    raise DeepSeekError("模型最终 JSON/业务约束连续校验失败，停止而非生成伪结果。") from None
                self.repairs += 1
                self.session.trace.record(state.agent_name, "output_validation_error", state.step, {"repair": self.repairs})
                self.messages.append({"role": "user", "content": "输出未通过本地 JSON/schema/事故ID/分类/日志读取校验。请先读取日志，再按指定 schema 修正，返回 JSON。"})
                continue
            self.session.trace.record(state.agent_name, "validated_output", state.step, _safe_value(output.model_dump(mode="json")))
            return FinalAction(output)
        call = self.pending.popleft()
        self.waiting_id = call["id"]
        function = call["function"]
        try:
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be an object")
        except (ValueError, TypeError):
            # A deliberately invalid argument reaches local schema validation;
            # the model receives an error under its original tool_call_id.
            arguments = {"invalid_json_arguments": True}
        return ToolAction(function["name"], _safe_value(arguments), decision_summary=f"DeepSeek tool_call_id={call['id']}")


class DeepSeekIncidentManager:
    """Code-owned gate -> model hypothesis -> model audit -> grounded report."""

    def __init__(self, config: DeepSeekConfig | None = None, transport: DeepSeekTransport | None = None):
        self.config = config or DeepSeekConfig()
        self.transport = transport or DeepSeekTransport()
        self.telemetry: dict[str, dict[str, Any]] = {}

    def diagnose(self, incident: Incident, mode: str = "multi", trace_dir: str | Path | None = None) -> DiagnosisReport:
        if mode not in {"single", "multi"}:
            raise ValueError("mode must be 'single' or 'multi'")
        trace = TraceRecorder()
        session = RequestSession(self.config, self.transport, trace)
        stats = session.telemetry
        self.telemetry[incident.incident_id] = stats
        report = None
        try:
            assessment = AnomalyDetector(admit_single_errors=True).assess(incident)
            trace.record("manager", "anomaly_gate", 0, assessment.model_dump(mode="json"))
            if not assessment.is_anomaly:
                report = DiagnosisReport(incident_id=incident.incident_id, mode=mode, is_anomaly=False,
                    anomaly_score=assessment.score, severity=Severity.INFO, confidence=0.96,
                    limitations=["规则异常门控未触发；本事故没有调用大模型。"])
            else:
                store = IncidentStore([incident])
                registry, schemas = build_model_tools(store, incident)
                allowed = {"get_incident_summary", "search_logs", "get_timeline", "get_runbook"}
                hypothesis_policy = DeepSeekPolicy(session, schemas, allowed, HypothesisBundle)
                bundle, calls = self._run("hypothesis_agent", hypothesis_policy, registry, trace, {
                    "incident_id": incident.incident_id, "instructions": HYPOTHESIS_AGENT_INSTRUCTIONS,
                })
                valid_ids = {event.line_id for event in incident.events}
                citations_before = sum(len(item.evidence) for item in bundle.hypotheses)
                for hypothesis in bundle.hypotheses:
                    hypothesis.evidence = [item for item in hypothesis.evidence if item.line_id in valid_ids & hypothesis_policy.seen_ids]
                trace.record("manager", "candidate_grounding", 90, {
                    "invalid_or_unread_citations_removed": citations_before - sum(len(item.evidence) for item in bundle.hypotheses),
                })
                primary = bundle.hypotheses[0]
                if mode == "multi":
                    auditor_policy = DeepSeekPolicy(session, schemas, {"get_events_by_ids", "get_timeline"}, AuditResult)
                    audit, audit_calls = self._run("evidence_auditor", auditor_policy, registry, trace, {
                        "incident_id": incident.incident_id, "hypothesis": primary.model_dump(mode="json"),
                        "instructions": EVIDENCE_AUDITOR_INSTRUCTIONS,
                    })
                    calls += audit_calls
                    candidate_by_id = {item.line_id: item for item in primary.evidence}
                    accepted_before = len(audit.accepted)
                    audit.accepted = [item for item in audit.accepted if item.line_id in auditor_policy.seen_ids
                        and item.line_id in candidate_by_id
                        and item.kind == candidate_by_id[item.line_id].kind]
                    trace.record("manager", "audit_grounding", 91, {
                        "unread_or_unmatched_citations_removed": accepted_before - len(audit.accepted),
                    })
                else:
                    audit = AuditResult(accepted=primary.evidence, rejected=[], confidence_multiplier=1.0,
                        notes=["single 模式没有模型证据审核；只执行代码引用校验。"])
                report = compose_audited_report(incident, assessment, bundle, audit, tool_calls=calls, mode=mode)
                if primary.code == "unknown" or not any(item.kind == "direct" for item in report.evidence):
                    report.root_cause_code = None
                    report.root_cause = "证据不足，暂不能确认根因"
                    report.confidence = min(report.confidence, 0.35)
                    runbook = store.get_runbook("unknown")
                    report.next_checks = list(runbook["checks"])
                    report.remediation = list(runbook["remediation"])
                    report.limitations.append("未取得审核通过的直接证据，代码将最终根因设为 unknown。")
                report.limitations.append("这是 DeepSeek 日志诊断原型；日志关联不等于因果证明，置信度不是校准概率。")
                trace.record("manager", "report_grounded", 99, {
                    "root_cause_code": report.root_cause_code, "evidence_ids": [item.line_id for item in report.evidence],
                })
            stats["status"] = "success"
            return report
        except Exception as exc:
            stats["status"] = "failed"
            stats["error"] = _safe(str(exc))[:1000]
            trace.record("manager", "diagnosis_error", 99, {"error": type(exc).__name__, "message": stats["error"]})
            raise
        finally:
            stats["latency_ms"] = round((time.perf_counter() - session.started) * 1000, 3)
            trace.record("manager", "run_telemetry", 100, dict(stats))
            if trace_dir is not None:
                target = trace.save(Path(trace_dir) / f"{trace.run_id}.jsonl")
                stats["trace_path"] = str(target.resolve())
                if report is not None:
                    report.trace_path = stats["trace_path"]

    def _run(self, actor: str, policy: DeepSeekPolicy, registry: ToolRegistry, trace: TraceRecorder, task: dict[str, Any]):
        steps = self.config.max_requests_per_agent * self.config.max_tools_per_response + 2
        return AgentRuntime(actor, policy, registry, trace, max_steps=steps,
            allowed_tools=policy.allowed, record_full_results=True).run(task)
