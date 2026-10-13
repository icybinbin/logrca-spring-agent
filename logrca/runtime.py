"""A tiny framework-independent Agent loop with observable tool execution."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, Protocol, TypeVar


OutputT = TypeVar("OutputT")


@dataclass(slots=True)
class ToolAction:
    name: str
    arguments: dict[str, Any]
    # A short observable justification, not hidden model chain-of-thought.
    decision_summary: str = ""


@dataclass(slots=True)
class FinalAction(Generic[OutputT]):
    output: OutputT


Action = ToolAction | FinalAction[OutputT]


@dataclass
class AgentState:
    agent_name: str
    task: dict[str, Any]
    observations: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0


class Policy(Protocol[OutputT]):
    def decide(self, state: AgentState) -> Action[OutputT]: ...


@dataclass
class TraceEvent:
    timestamp: str
    run_id: str
    actor: str
    event_type: str
    step: int
    details: dict[str, Any]
    duration_ms: float | None = None


class TraceRecorder:
    """Captures the complete decision/tool trajectory for debugging and evals."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.events: list[TraceEvent] = []

    def record(
        self,
        actor: str,
        event_type: str,
        step: int,
        details: dict[str, Any],
        duration_ms: float | None = None,
    ) -> None:
        self.events.append(
            TraceEvent(
                timestamp=datetime.now(timezone.utc).isoformat(),
                run_id=self.run_id,
                actor=actor,
                event_type=event_type,
                step=step,
                details=details,
                duration_ms=round(duration_ms, 3) if duration_ms is not None else None,
            )
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")
        return target


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}

    def register(self, name: str, function: Callable[..., Any]) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = function

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc
        return tool(**arguments)


class AgentRuntime(Generic[OutputT]):
    """Runs decide -> tool -> observe until the policy emits a typed result."""

    def __init__(
        self,
        agent_name: str,
        policy: Policy[OutputT],
        tools: ToolRegistry,
        trace: TraceRecorder,
        max_steps: int = 8,
        allowed_tools: set[str] | None = None,
        record_full_results: bool = False,
    ) -> None:
        self.agent_name = agent_name
        self.policy = policy
        self.tools = tools
        self.trace = trace
        self.max_steps = max_steps
        self.allowed_tools = allowed_tools
        self.record_full_results = record_full_results

    def run(self, task: dict[str, Any]) -> tuple[OutputT, int]:
        state = AgentState(agent_name=self.agent_name, task=task)
        tool_results_before_run = sum(
            event.actor == self.agent_name and event.event_type == "tool_result"
            for event in self.trace.events
        )
        self.trace.record(self.agent_name, "agent_start", 0, {"task": task})
        for step in range(1, self.max_steps + 1):
            state.step = step
            action = self.policy.decide(state)
            if isinstance(action, FinalAction):
                self.trace.record(
                    self.agent_name,
                    "agent_final",
                    step,
                    {"output_type": type(action.output).__name__},
                )
                tool_results_after_run = sum(
                    event.actor == self.agent_name and event.event_type == "tool_result"
                    for event in self.trace.events
                )
                return action.output, tool_results_after_run - tool_results_before_run

            self.trace.record(
                self.agent_name,
                "tool_call",
                step,
                {
                    "tool": action.name,
                    "arguments": action.arguments,
                    "decision_summary": action.decision_summary,
                },
            )
            started = time.perf_counter()
            if self.allowed_tools is not None and action.name not in self.allowed_tools:
                # Authorization lives in runtime code.  Prompt instructions are
                # useful behavior guidance but are not a security boundary.
                result = {
                    "error": "ToolPermissionError",
                    "message": f"{self.agent_name} may not call {action.name}",
                }
            else:
                try:
                    result = self.tools.call(action.name, action.arguments)
                except Exception as exc:  # Agent observes bounded tool errors and can recover.
                    result = {"error": type(exc).__name__, "message": str(exc)}
            duration_ms = (time.perf_counter() - started) * 1000
            observation = {"tool": action.name, "arguments": action.arguments, "result": result}
            state.observations.append(observation)
            preview = json.dumps(result, ensure_ascii=False, default=str)
            self.trace.record(
                self.agent_name,
                "tool_result",
                step,
                {
                    "tool": action.name,
                    "preview": preview[:1200],
                    "result_characters": len(preview),
                    "preview_truncated": len(preview) > 1200,
                    **({"result": result} if self.record_full_results else {}),
                },
                duration_ms,
            )
        raise RuntimeError(f"{self.agent_name} exceeded max_steps={self.max_steps}")
