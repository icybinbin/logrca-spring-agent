"""Run the first offline Agent lesson."""

from __future__ import annotations

import json

from agent_core import DemoModel, OfflineLogAgent, ToolRegistry
from log_tools import get_service_stats, search_logs


def build_agent() -> OfflineLogAgent:
    tools = ToolRegistry()
    tools.register("search_logs", search_logs)
    tools.register("get_service_stats", get_service_stats)
    return OfflineLogAgent(model=DemoModel(), tools=tools)


if __name__ == "__main__":
    agent = build_agent()
    result = agent.run("分析最近一次订单接口异常，并给出证据和下一步排查建议")

    print("=== Agent trace ===")
    for item in result.trace:
        print(json.dumps(item, ensure_ascii=False))

    print("\n=== Final structured output ===")
    print(json.dumps(result.final_output, ensure_ascii=False, indent=2))
