"""Zen 429 深挖诊断：区分「额度耗尽」与「出口 IP / 路径差异」。

对比三条出口路径（直连 / 系统代理 127.0.0.1:7897 / 强制重试）下的行为，
并打印完整响应头——429 是应用层（Zen）还是边缘（Cloudflare）一看便知。

背景（2026-10-02 实测）：本机直连出口的 429 FreeUsageLimitError 随 UTC 日
重置恢复；且直连 opencode.ai 存在间歇性 SSL EOF（连接被重置），探测必须
带重试，否则会把网络抖动误读成服务端行为。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
PROMPT = (REPO / "proxy_opencode/upstreams/zen_prompt_default.txt").read_text(encoding="utf-8")
TOOLS_PATH = REPO / "proxy_opencode/upstreams/zen_builtin_tools.json"
TOOLS = json.loads(TOOLS_PATH.read_text(encoding="utf-8"))
if isinstance(TOOLS, dict):
    TOOLS = TOOLS.get("tools", [])

HDRS = {
    "Authorization": "Bearer public",
    "User-Agent": "opencode/1.18.32 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
    "x-opencode-client": "cli",
    "x-opencode-project": "global",
    "x-opencode-session": "ses_abc123def45678901234567890",
    "x-opencode-request": "msg_abc123def45678901234567890",
}

_INTERESTING_HEADERS = {
    "server", "cf-ray", "cf-mitigated", "cf-cache-status", "retry-after",
    "content-type", "date",
}


def _chat_body(model: str) -> dict:
    return {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": "reply: pong"},
        ],
        "tools": TOOLS,
    }


def probe_chat(label: str, client: httpx.Client, model: str, attempts: int = 4) -> None:
    for i in range(attempts):
        try:
            r = client.post(
                "https://opencode.ai/zen/v1/chat/completions",
                json=_chat_body(model), headers=HDRS, timeout=60,
            )
            print(f"[{label}] {model} -> {r.status_code}")
            for k, v in r.headers.items():
                if k.lower() in _INTERESTING_HEADERS or "ratelimit" in k.lower():
                    print(f"      {k}: {v}")
            print(f"      body: {r.text[:200].strip()}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] {model} attempt {i}: {type(e).__name__}: {e}")
            time.sleep(1.5)
    print(f"[{label}] {model} -> ALL ATTEMPTS FAILED")


def probe_models(label: str, client: httpx.Client, attempts: int = 3) -> None:
    for i in range(attempts):
        try:
            r = client.get("https://opencode.ai/zen/v1/models", headers=HDRS, timeout=30)
            print(f"[{label}] GET /models -> {r.status_code} ({len(r.text)} bytes)")
            return
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] GET /models attempt {i}: {type(e).__name__}: {e}")
            time.sleep(1.5)


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "big-pickle"

    print("== direct (trust_env=False) ==")
    with httpx.Client(trust_env=False) as c:
        probe_chat("direct", c, model)

    print("== system proxy 127.0.0.1:7897 ==")
    try:
        with httpx.Client(trust_env=False, proxy="http://127.0.0.1:7897") as c:
            probe_chat("proxy", c, model)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] cannot prime client: {type(e).__name__}: {e}")

    print("== models endpoint sanity (direct) ==")
    with httpx.Client(trust_env=False) as c:
        probe_models("direct", c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
