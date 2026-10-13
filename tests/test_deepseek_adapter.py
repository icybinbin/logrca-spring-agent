from __future__ import annotations

import copy
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from logrca.deepseek_adapter import (
    DeepSeekConfig, DeepSeekError, DeepSeekIncidentManager, DeepSeekTransport,
    ModelBudgetError,
)
from logrca.pipeline import analyze_path
from logrca.spring_parser import parse_spring_log
from logrca.synthetic import build_scenario


class ScriptedTransport:
    """Protocol fixture, not a model-quality evaluation or paid API test."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, body, *, timeout):
        self.requests.append(copy.deepcopy(body))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def tool(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {
        "name": name,
        "arguments": json.dumps(arguments) if not isinstance(arguments, str) else arguments,
    }}


def response(*, calls=None, output=None, finish=None):
    return {
        "model": "deepseek-flash-test-response",
        "choices": [{"finish_reason": finish or ("tool_calls" if calls else "stop"),
            "message": {"role": "assistant", "content": json.dumps(output) if output is not None else None,
                **({"tool_calls": calls} if calls else {})}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
            "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 10},
    }


def refs(incident):
    direct = next(event for event in incident.events if event.template_id == "DB_POOL_EXHAUSTED")
    support = next(event for event in incident.events if event.template_id == "DB_CONNECTION_FAILED")
    symptom = next(event for event in incident.events if event.template_id == "GATEWAY_5XX")
    return [{"line_id": event.line_id, "kind": kind, "summary": "model-controlled text", "weight": weight}
        for event, kind, weight in [(direct, "direct", .9), (support, "support", .7), (symptom, "symptom", .2)]]


def bundle(incident, evidence=None, code="db_pool_exhausted"):
    return {"incident_id": incident.incident_id, "hypotheses": [{"code": code,
        "title": "连接池获取连接超时", "score": .9, "reasoning": "基于日志的待验证判断",
        "evidence": evidence if evidence is not None else refs(incident)}],
        "remediation": ["model wants unsafe production change"]}


def scripted_diagnosis(incident):
    evidence = refs(incident)
    fabricated = {**evidence[0], "line_id": "NOT-IN-SOURCE"}
    return [
        response(calls=[tool("h1", "get_incident_summary", {}), tool("h2", "search_logs", {"limit": 20})]),
        response(calls=[tool("h3", "get_timeline", {}), tool("h4", "get_runbook", {"root_cause_code": "db_pool_exhausted"})]),
        response(output=bundle(incident, evidence + [fabricated])),
        response(calls=[tool("a1", "get_events_by_ids", {"line_ids": [item["line_id"] for item in evidence]})]),
        # Attempt to promote a gateway symptom to direct evidence, and cite an invented ID.
        response(output={"accepted": evidence[:2] + [{**evidence[2], "kind": "direct"}, fabricated],
            "rejected": [], "confidence_multiplier": .8, "notes": []}),
    ]


def test_real_protocol_multi_calls_grounding_and_telemetry(tmp_path):
    incident = build_scenario("db_pool_exhausted").incident
    incident.events[2].message += " password=secret-pass"
    transport = ScriptedTransport(scripted_diagnosis(incident))
    manager = DeepSeekIncidentManager(transport=transport)
    report = manager.diagnose(incident, trace_dir=tmp_path)

    assert report.root_cause_code == "db_pool_exhausted"
    assert report.confidence == .72
    assert report.tool_calls == 5
    assert [item.line_id for item in report.evidence] == [item["line_id"] for item in refs(incident)[:2]]
    assert "model-controlled text" not in report.evidence[0].summary
    assert "unsafe production change" not in str(report.remediation)
    assert report.trace_path is not None

    # Both tool results, with matching provider IDs, precede the NEXT request.
    second_messages = transport.requests[1]["messages"]
    assert [item["tool_call_id"] for item in second_messages if item["role"] == "tool"] == ["h1", "h2"]
    assert second_messages[-2]["role"] == second_messages[-1]["role"] == "tool"
    search_result = json.loads(second_messages[-1]["content"])
    assert all("template_id" not in row for row in search_result)
    assert "secret-pass" not in json.dumps(transport.requests)
    assert transport.requests[0]["thinking"] == {"type": "disabled"}
    assert transport.requests[0]["response_format"] == {"type": "json_object"}
    audit_request = transport.requests[3]
    assert {item["function"]["name"] for item in audit_request["tools"]} == {"get_events_by_ids", "get_timeline"}
    stats = manager.telemetry[incident.incident_id]
    assert stats["requests"] == stats["completed_requests"] == 5
    assert stats["total_tokens"] == 80
    assert stats["prompt_tokens"] == 60
    assert stats["completion_tokens"] == 20
    assert stats["cache_hit_tokens"] == 10
    trace_text = Path(report.trace_path).read_text(encoding="utf-8")
    assert "secret-pass" not in trace_text
    events = [json.loads(line) for line in trace_text.splitlines()]
    assert any(event["event_type"] == "tool_result" and "result" in event["details"] for event in events)
    assert sum(event["event_type"] == "validated_output" for event in events) == 2


def test_invalid_tool_arguments_are_rejected_and_returned_to_model(tmp_path):
    incident = build_scenario("db_pool_exhausted").incident
    transport = ScriptedTransport([
        response(calls=[
            tool("bad-json", "search_logs", "{broken"),
            tool("bad-limit", "search_logs", {"limit": 10000}),
            tool("other-incident", "search_logs", {"incident_id": "OTHER"}),
            tool("forbidden", "execute_shell", {"command": "delete data"}),
        ]),
        response(calls=[tool("good", "search_logs", {})]),
        response(output=bundle(incident)),
    ])
    report = DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single", trace_dir=tmp_path)
    assert report.root_cause_code == "db_pool_exhausted"
    errors = [json.loads(item["content"]) for item in transport.requests[1]["messages"] if item["role"] == "tool"]
    assert len(errors) == 4
    assert [error["error"] for error in errors] == ["ValueError", "ValueError", "ValueError", "ToolPermissionError"]


def test_wrong_types_and_cross_incident_ids_are_rejected():
    incident = build_scenario("db_pool_exhausted").incident
    transport = ScriptedTransport([
        response(calls=[tool("string-limit", "search_logs", {"limit": "20"}),
            tool("foreign-id", "get_timeline", {"line_ids": ["FOREIGN-LOG-ID"]})]),
        response(calls=[tool("good", "search_logs", {})]),
        response(output=bundle(incident)),
    ])
    DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single")
    results = [json.loads(item["content"]) for item in transport.requests[1]["messages"] if item["role"] == "tool"]
    assert all(item["error"] == "ValueError" for item in results)


def test_normal_incident_has_no_model_requests_or_key_requirement(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    transport = ScriptedTransport([])
    incident = build_scenario("normal").incident
    manager = DeepSeekIncidentManager(transport=transport)
    assert manager.diagnose(incident).is_anomaly is False
    assert transport.requests == []
    assert manager.telemetry[incident.incident_id]["total_tokens"] == 0


def test_single_gateway_error_enters_model_path_and_abstains():
    base = build_scenario("db_pool_exhausted").incident
    symptom = next(event for event in base.events if event.template_id == "GATEWAY_5XX")
    incident = base.model_copy(update={"events": [symptom]})
    evidence = [{"line_id": symptom.line_id, "kind": "symptom", "summary": "503", "weight": .2}]
    transport = ScriptedTransport([response(calls=[tool("logs", "search_logs", {})]),
        response(output=bundle(incident, evidence, "unknown"))])
    report = DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single")
    assert report.is_anomaly is True
    assert report.root_cause_code is None
    assert len(transport.requests) == 2


@pytest.mark.parametrize("evidence_mode", ["invented", "unread", "symptom", "unknown"])
def test_insufficient_evidence_abstains_instead_of_confirming_root(evidence_mode):
    incident = build_scenario("db_pool_exhausted").incident
    evidence = refs(incident)
    query = {}
    code = "db_pool_exhausted"
    if evidence_mode == "invented":
        evidence = [{**evidence[0], "line_id": "FABRICATED"}]
    elif evidence_mode == "unread":
        query = {"levels": ["INFO"]}
        evidence = evidence[:1]
    elif evidence_mode == "symptom":
        evidence = evidence[2:]
    else:
        code = "unknown"
    transport = ScriptedTransport([response(calls=[tool("logs", "search_logs", query)]),
        response(output=bundle(incident, evidence, code))])
    report = DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single")
    assert report.root_cause_code is None
    assert report.confidence <= .35
    assert "补充指标" in report.next_checks[0]


def test_schema_repair_is_bounded_and_trace_persists_on_failure(tmp_path):
    incident = build_scenario("db_pool_exhausted").incident
    transport = ScriptedTransport([response(calls=[tool("logs", "search_logs", {})]),
        response(output={"bad": "contract"}), response(output={"bad": "contract"})])
    manager = DeepSeekIncidentManager(transport=transport)
    with pytest.raises(DeepSeekError, match="连续校验失败"):
        manager.diagnose(incident, trace_dir=tmp_path)
    assert len(transport.requests) == 3
    stats = manager.telemetry[incident.incident_id]
    assert stats["status"] == "failed"
    events = [json.loads(line) for line in Path(stats["trace_path"]).read_text(encoding="utf-8").splitlines()]
    assert sum(item["event_type"] == "output_validation_error" for item in events) == 1
    assert events[-1]["event_type"] == "run_telemetry"


def test_schema_repair_can_recover():
    incident = build_scenario("db_pool_exhausted").incident
    wrong_id = {**bundle(incident), "incident_id": "WRONG-INCIDENT"}
    transport = ScriptedTransport([response(calls=[tool("logs", "search_logs", {})]),
        response(output=wrong_id), response(output=bundle(incident))])
    report = DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single")
    assert report.incident_id == incident.incident_id
    assert report.root_cause_code == "db_pool_exhausted"


@pytest.mark.parametrize("config", [DeepSeekConfig(max_requests=1), DeepSeekConfig(max_total_tokens=1), DeepSeekConfig(max_requests_per_agent=1)])
def test_model_budgets_stop_additional_requests(config, tmp_path):
    incident = build_scenario("db_pool_exhausted").incident
    transport = ScriptedTransport([response(calls=[tool("logs", "search_logs", {})])])
    manager = DeepSeekIncidentManager(config=config, transport=transport)
    with pytest.raises(ModelBudgetError):
        manager.diagnose(incident, trace_dir=tmp_path)
    assert len(transport.requests) == 1
    assert manager.telemetry[incident.incident_id]["status"] == "failed"
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


def test_truncated_json_is_not_accepted(tmp_path):
    incident = build_scenario("db_pool_exhausted").incident
    transport = ScriptedTransport([response(output=bundle(incident), finish="length")])
    with pytest.raises(DeepSeekError, match="未正常完成"):
        DeepSeekIncidentManager(transport=transport).diagnose(incident, mode="single", trace_dir=tmp_path)


def test_missing_key_produces_explicit_failure_and_partial_trace(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    incident = build_scenario("db_pool_exhausted").incident
    manager = DeepSeekIncidentManager()
    with pytest.raises(DeepSeekError, match="DEEPSEEK_API_KEY"):
        manager.diagnose(incident, trace_dir=tmp_path)
    assert manager.telemetry[incident.incident_id]["status"] == "failed"
    assert manager.telemetry[incident.incident_id]["completed_requests"] == 0
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


def test_file_pipeline_uses_model_backend_and_saves_usage(monkeypatch, tmp_path):
    source = Path(__file__).parents[1] / "data" / "spring_db_pool.log"
    incident = parse_spring_log(source)
    # Correlation owns the ID, so get the same incident used in the file pipeline.
    from logrca.correlation import build_incidents
    incident = build_incidents(incident.events)[0]
    scripted = ScriptedTransport(scripted_diagnosis(incident))
    monkeypatch.setattr(DeepSeekTransport, "complete", scripted.complete)
    result = analyze_path(source, backend="deepseek", output_dir=tmp_path)
    assert result.successful_incident_count == 1
    assert result.reports[0].root_cause_code == "db_pool_exhausted"
    summary = json.loads(Path(result.run_summary_path).read_text(encoding="utf-8"))
    assert summary["backend"] == "deepseek"
    assert summary["model_limits"]["max_requests"] == 12
    assert summary["model_telemetry"][incident.incident_id]["total_tokens"] == 80
    assert summary["reports"][0]["trace_path"]


def test_provider_failure_is_saved_without_offline_fallback(monkeypatch, tmp_path):
    source = Path(__file__).parents[1] / "data" / "spring_db_pool.log"
    scripted = ScriptedTransport([DeepSeekError("simulated provider unavailable")])
    monkeypatch.setattr(DeepSeekTransport, "complete", scripted.complete)
    result = analyze_path(source, backend="deepseek", output_dir=tmp_path)
    assert result.failed_incident_count == 1
    assert result.reports == []
    assert result.errors[0].stage == "diagnose"
    stats = next(iter(result.model_telemetry.values()))
    assert stats["status"] == "failed"
    assert Path(stats["trace_path"]).is_file()


def test_http_transport_uses_official_endpoint_and_timeout(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key-not-a-real-credential")
    captured = {}

    def open_request(request, *, timeout):
        captured.update(url=request.full_url, method=request.method, timeout=timeout,
            body=json.loads(request.data), authorization=request.get_header("Authorization"))
        return io.BytesIO(json.dumps(response(output={"ok": True})).encode())

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    body = {"model": "deepseek-flash", "messages": [{"role": "user", "content": "JSON"}]}
    payload = DeepSeekTransport().complete(body, timeout=3.5)
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 3.5
    assert captured["body"] == body
    assert captured["authorization"] == "Bearer test-key-not-a-real-credential"
    assert payload["model"] == "deepseek-flash-test-response"


def test_http_error_redacts_key_and_network_timeout_is_explicit(monkeypatch):
    key = "test-secret-with-no-special-prefix"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)

    def rejected(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.deepseek.com/chat/completions", 401,
            "Unauthorized", {}, io.BytesIO(f"{key} password=private-value".encode()))

    monkeypatch.setattr(urllib.request, "urlopen", rejected)
    with pytest.raises(DeepSeekError) as raised:
        DeepSeekTransport().complete({}, timeout=1)
    assert "HTTP 401" in str(raised.value)
    assert key not in str(raised.value)
    assert "private-value" not in str(raised.value)

    def timed_out(*args, **kwargs):
        raise TimeoutError("simulated network timeout")

    monkeypatch.setattr(urllib.request, "urlopen", timed_out)
    with pytest.raises(DeepSeekError, match="TimeoutError"):
        DeepSeekTransport().complete({}, timeout=1)
