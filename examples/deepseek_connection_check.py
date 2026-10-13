"""Check one real DeepSeek API call before integrating the Agent loop.

This is an API connectivity check, not the complete LogRCA Agent.
The key comes from DEEPSEEK_API_KEY; it is never printed or written to disk.
Defaults verified against official DeepSeek documentation on 2026-10-10:
https://api-docs.deepseek.com/zh-cn/
https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/

Run from agent_log_demo:
    python -X utf8 examples/deepseek_connection_check.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.request
from typing import Any


def safe_error_text(value: str, api_key: str) -> str:
    """Keep useful diagnostics without echoing credentials."""
    value = value.replace(api_key, "[REDACTED]") if api_key else value
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", value)
    value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    return value[:1200]


def request_without_sdk(api_key: str, model: str) -> dict[str, Any]:
    """Use Python's standard HTTP client to isolate SDK compatibility issues."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "用适合初学者的简短中文回答。"},
            {"role": "user", "content": "用两句话解释数据库连接池是什么。"},
        ],
        "max_tokens": 300,
        "thinking": {"type": "disabled"},
    }
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30.0) as response:
        return json.loads(response.read().decode("utf-8"))


def request_with_sdk(api_key: str, model: str) -> dict[str, Any]:
    from openai import OpenAI

    # OpenAI is the client library name. Requests go to DeepSeek, and use
    # your DeepSeek key and balance. No OpenAI account/key is needed here.
    with OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
        timeout=30.0,
        max_retries=0,
    ) as client:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "用适合初学者的简短中文回答。"},
                {"role": "user", "content": "用两句话解释数据库连接池是什么。"},
            ],
            max_tokens=300,
            extra_body={"thinking": {"type": "disabled"}},
        )
    return response.model_dump()


def main() -> int:
    parser = argparse.ArgumentParser(description="Make one small DeepSeek API request")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument(
        "--transport", choices=("sdk", "urllib"), default="sdk",
        help="urllib uses Python's built-in HTTP client without the OpenAI SDK",
    )
    args = parser.parse_args()

    # api_key = authentication; base_url = provider address; model = model ID.
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("未设置 DEEPSEEK_API_KEY。请先在本地 PowerShell 中设置环境变量。")
        return 2

    print(f"请求方式：{args.transport}；请求模型：{args.model}")
    started = time.perf_counter()
    try:
        if args.transport == "urllib":
            response = request_without_sdk(api_key, args.model)
        else:
            response = request_with_sdk(api_key, args.model)
        content = response["choices"][0]["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("响应中没有可读的模型回答")
    except ImportError:
        print("缺少客户端。运行：python -m pip install openai；或使用 --transport urllib")
        return 2
    except Exception as exc:
        status = getattr(exc, "status_code", getattr(exc, "code", "无"))
        print(f"调用失败：{type(exc).__name__}，HTTP 状态={status}。")
        print("具体原因：", safe_error_text(str(exc), api_key))
        if isinstance(exc, urllib.error.HTTPError):
            detail = exc.read(4096).decode("utf-8", errors="replace")
            print("服务端说明：", safe_error_text(detail, api_key))
        if exc.__cause__ is not None:
            print("底层原因：", safe_error_text(str(exc.__cause__), api_key))
        # Frame locations contain no local values or authentication headers.
        for frame in traceback.extract_tb(exc.__traceback__)[-4:]:
            print(f"错误位置：{frame.filename}:{frame.lineno} ({frame.name})")
        return 1

    print(f"返回模型：{response.get('model', '服务端未提供')}")
    print(content)
    print(f"耗时：{time.perf_counter() - started:.2f} 秒")
    print("Token 用量：", response.get("usage") or "服务端未提供")
    print("此结果仅证明 API 连通；下一步还需实现工具调用与证据校验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
