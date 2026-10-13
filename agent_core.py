"""Stage 1: a tiny, offline Agent loop for learning.

This file intentionally does not call a language model.  ``DemoModel`` plays
the role of the model's planner so that the control flow is visible and
testable without an API key or token cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


DEFAULT_INSTRUCTIONS = """
你是日志诊断助手。先通过工具查询有限的日志证据，再判断异常、根因和下一步检查。
不得把没有日志证据支持的推测写成确定事实；最终输出必须包含 line_id 证据。
""".strip()


@dataclass
class ToolCall:
    """A request from the model to the runtime to call one tool."""

    name: str
    arguments: Dict[str, Any]


@dataclass
class FinalAnswer:
    """The model's final structured answer."""

    payload: Dict[str, Any]


@dataclass
class AgentState:
    """The small amount of state carried through one Agent run."""

    user_request: str
    instructions: str
    observations: List[Dict[str, Any]] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)


class ToolRegistry:
    """Runtime-owned registry: the model chooses a name, Python executes it."""

    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Dict[str, Any]]] = {}

    def register(self, name: str, function: Callable[..., Dict[str, Any]]) -> None:
        self._tools[name] = function

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")
        return self._tools[name](**arguments)


class DemoModel:
    """Deterministic stand-in for an LLM planner.

    The sequence is deliberately explicit for teaching:

    1. search error logs;
    2. query service statistics;
    3. produce a structured diagnosis.

    A real model would choose these actions from instructions, tool schemas,
    and the current conversation state.
    """

    def next_action(self, state: AgentState) -> ToolCall | FinalAnswer:
        if not state.observations:
            return ToolCall(
                name="search_logs",
                arguments={"keyword": "ERROR", "limit": 8},
            )

        if len(state.observations) == 1:
            error_result = state.observations[0]["result"]
            services = [item["service"] for item in error_result["events"]]
            service = "order-service" if "order-service" in services else services[0]
            return ToolCall(
                name="get_service_stats",
                arguments={"service": service},
            )

        error_result = state.observations[0]["result"]
        stats_result = state.observations[1]["result"]
        events = error_result["events"]
        root_cause_event = next(
            (event for event in events if event["template_id"] == "DB_POOL_EXHAUSTED"),
            events[0],
        )
        related_events = [event for event in events if event["line_id"] != root_cause_event["line_id"]]

        return FinalAnswer(
            payload={
                "is_anomaly": True,
                "severity": "high",
                "suspected_root_cause": "order-service 数据库连接池耗尽",
                "confidence": 0.86,
                "affected_service": stats_result["service"],
                "evidence": [
                    {
                        "line_id": root_cause_event["line_id"],
                        "trace_id": root_cause_event["trace_id"],
                        "why": "最早出现连接池耗尽的直接错误",
                    },
                    *[
                        {
                            "line_id": event["line_id"],
                            "trace_id": event["trace_id"],
                            "why": "同一故障窗口中的级联症状",
                        }
                        for event in related_events[:3]
                    ],
                ],
                "next_checks": [
                    "检查 HikariCP active/idle/max 数据库连接指标",
                    "检查数据库慢查询和连接泄漏",
                    "确认 gateway 5xx 是否随连接池恢复而下降",
                ],
                "tool_summary": {
                    "error_count": len(events),
                    "service_error_rate": stats_result["error_rate"],
                },
            }
        )


@dataclass
class AgentResult:
    final_output: Dict[str, Any]
    trace: List[Dict[str, Any]]
    steps: int


class OfflineLogAgent:
    """The runtime that owns the loop and executes tools."""

    def __init__(
        self,
        model: DemoModel,
        tools: ToolRegistry,
        instructions: str = DEFAULT_INSTRUCTIONS,
        max_steps: int = 5,
    ) -> None:
        self.model = model
        self.tools = tools
        self.instructions = instructions
        self.max_steps = max_steps

    def run(self, user_request: str) -> AgentResult:
        # In a real SDK, instructions are sent as the system/developer policy
        # and user_request is the task for this specific run.
        state = AgentState(
            user_request=user_request,
            instructions=self.instructions,
        )

        for step in range(1, self.max_steps + 1):
            # ① Ask the model/planner what should happen next.  A real LLM
            #    would inspect instructions plus the current state here.
            action = self.model.next_action(state)

            if isinstance(action, FinalAnswer):
                # ② The model has decided that it has enough evidence.  The
                #    runtime records the end of the trace and returns data to
                #    the caller instead of continuing forever.
                state.trace.append(
                    {"step": step, "actor": "model", "type": "final_answer"}
                )
                return AgentResult(action.payload, state.trace, step)

            # ③ The model does not execute Python.  It emits an intent:
            #    "call this named tool with these arguments".
            state.trace.append(
                {
                    "step": step,
                    "actor": "model",
                    "type": "tool_call",
                    "tool": action.name,
                    "arguments": action.arguments,
                }
            )

            # ④ The application/runtime owns execution, validation and
            #    permissions.  This boundary is essential in production.
            result = self.tools.call(action.name, action.arguments)

            # ⑤ Feed the observation back into the state.  The next loop
            #    iteration can now make a different decision using this data.
            observation = {"tool": action.name, "result": result}
            state.observations.append(observation)
            state.trace.append(
                {
                    "step": step,
                    "actor": "runtime",
                    "type": "tool_result",
                    "tool": action.name,
                    "result_preview": result,
                }
            )

        raise RuntimeError("Agent stopped without a final answer")
