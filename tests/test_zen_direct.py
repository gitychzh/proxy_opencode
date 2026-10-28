"""Tests for the zen-direct adapter (protocol reconstruction)."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import httpx
import pytest
import respx

from proxy_opencode.config import Settings
from proxy_opencode.egress.pool import EgressSpec, EgressState, Lease
from proxy_opencode.upstreams import zen as zen_direct
from proxy_opencode.upstreams.openai_http import StreamRelay
from proxy_opencode.upstreams.zen import ZenDirectAdapter


def make_settings(**kw: Any) -> Settings:
    s = Settings(upstream_mode="zen-direct", **kw)
    return s


# --------------------------------------------------------------------- ids


def test_opencode_id_format_matches_opencode_schema() -> None:
    sid = zen_direct.opencode_id("ses_")
    assert sid.startswith("ses_")
    body = sid[4:]
    assert len(body) == 26
    int(body[:12], 16)  # time part is hex
    assert all(c.isalnum() for c in body[12:])
    assert zen_direct.is_valid_opencode_id(sid, "ses_")
    assert zen_direct.is_valid_opencode_id("msg_0da069dc1001mKuBwHfaKkaZSM", "msg_")
    # captured-fake id from the 2026-09 probe that got FreeTierError
    assert not zen_direct.is_valid_opencode_id("ses_ztest000001aaaaaaaaaaaaa", "ses_")


def test_opencode_id_time_part_matches_db_pairs() -> None:
    # Pairs taken from real opencode client DBs (id prefix vs time.created ms):
    # time part == ((ms << 12) + counter) & 0xFFFFFFFFFFFF, counter starts at 1.
    # Message ids are ascending; SESSION ids are descending since 1.19.0
    # (schema/src/session-id.ts mints with descending()).
    ascending_pairs = [
        # opencode 1.18.32 session ids (ascending era), local opencode.db
        ("0e198de83001", 1790491287171),
        ("0e18e1c44001", 1790490582084),
        ("0e17f517e001", 1790489612670),
    ]
    for time_part, created_ms in ascending_pairs:
        assert int(time_part, 16) == ((created_ms << 12) + 1) & 0xFFFFFFFFFFFF
    descending_pairs = [
        # opencode 1.19.0 session ids (descending era), local opencode.db
        ("ecc618ef6ffe", 1791867384073),
        ("ecc5f84b6ffe", 1791867517769),
    ]
    for time_part, created_ms in descending_pairs:
        assert int(time_part, 16) == (~((created_ms << 12) + 1)) & 0xFFFFFFFFFFFF


def _unwrap_ms(low36: int, now_ms: int) -> int:
    """Map a 36-bit-truncated ms back near now_ms (id time part loses high bits)."""
    period = 1 << 36
    return low36 + round((now_ms - low36) / period) * period


def test_session_ids_are_descending_with_counter_below_64() -> None:
    # Free-tier gate (live-verified 2026-10-13): x-opencode-session must be
    # descending-encoded and its per-ms counter must stay below 64; ascending
    # session ids get HTTP 403 FreeTierError.
    for _ in range(50):
        sid = zen_direct.opencode_id("ses_", descending=True)
        body = sid[4:]
        assert len(body) == 26
        raw = int(body[:12], 16)
        current = (~raw) & 0xFFFFFFFFFFFF  # invert back to ascending space
        counter = current & 0xFFF
        assert 1 <= counter < 64
        now_ms = time.time_ns() // 1_000_000
        assert abs(_unwrap_ms(current >> 12, now_ms) - now_ms) < 60_000
        # request ids stay ascending (no counter limit on the gate)
        rid = zen_direct.opencode_id("msg_")
        assert abs(_unwrap_ms(int(rid[4:16], 16) >> 12, now_ms) - now_ms) < 60_000
        # an ascending session id decodes differently from a descending one
        assert zen_direct.opencode_id("ses_", descending=True) != zen_direct.opencode_id("ses_")


# ----------------------------------------------------------------- headers


@pytest.mark.asyncio
async def test_headers_reconstruct_captured_protocol() -> None:
    adapter = ZenDirectAdapter(make_settings())
    headers = adapter._headers()
    assert headers["Authorization"] == "Bearer public"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == (
        "opencode/1.19.4 ai-sdk/provider-utils/4.0.23 runtime/bun/1.4.4"
    )
    assert headers["x-opencode-client"] == "cli"
    # The genuine 1.19.4 client sends a 40-hex workspace hash here (never
    # the legacy "global" literal); the gateway mirrors it with a fixed
    # instance-level sha1 when OPENCODE_ZEN_PROJECT_ID is unset.
    assert re.fullmatch(r"[0-9a-f]{40}", headers["x-opencode-project"])
    # The captured shape carries the minted session id TWICE and never the
    # legacy `x-session-id` stable header nor a duplicated request id.
    assert headers["x-opencode-session-id"] == headers["x-opencode-session"]
    assert "x-session-id" not in headers
    assert "x-opencode-request-id" not in headers
    assert zen_direct.is_valid_opencode_id(headers["x-opencode-session"], "ses_")
    assert zen_direct.is_valid_opencode_id(headers["x-opencode-request"], "msg_")
    # session id must be the descending encoding (2026-10-13 gate); request id ascending
    ses_raw = int(headers["x-opencode-session"][4:16], 16)
    ses_current = (~ses_raw) & 0xFFFFFFFFFFFF
    assert 1 <= ses_current & 0xFFF < 64
    now_ms = time.time_ns() // 1_000_000
    assert abs(_unwrap_ms(ses_current >> 12, now_ms) - now_ms) < 60_000
    req_raw = int(headers["x-opencode-request"][4:16], 16)
    assert abs(_unwrap_ms(req_raw >> 12, now_ms) - now_ms) < 60_000
    await adapter.aclose()


@pytest.mark.asyncio
async def test_headers_use_api_key_when_configured() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    assert adapter._headers()["Authorization"] == "Bearer oc_sk_test"
    await adapter.aclose()


# -------------------------------------------------------------- body build


@pytest.mark.asyncio
async def test_body_bridge_note_by_default_and_merges_builtin_tools() -> None:
    """Explicit bridge mode: tiny steering note, no 12.8k prompt."""
    adapter = ZenDirectAdapter(make_settings(zen_marker_mode="bridge"))
    body = adapter._build_body(
        {
            "model": "opencode/big-pickle",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
        },
        "big-pickle",
    )
    assert body["model"] == "big-pickle"
    assert body["temperature"] == 0.5
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert "OpenAI-compatible API client" in msgs[0]["content"]
    assert "NEVER call the opencode built-in tools" in msgs[0]["content"]
    # the fat opencode default prompt must NOT be injected
    assert "interactive CLI tool" not in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}
    # free tier gate (2026-10-24): >= 4 real builtin names must be present
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"bash", "edit", "glob", "read"} <= names
    assert body["tool_choice"] == "auto"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_default_marker_mode_is_full_genuine_prompt() -> None:
    """Round 5 (2026-10-25 live ablation): the upstream now gates the BODY —
    a tiny system note (old default "bridge") gets 429 FreeUsageLimitError
    even on a fresh session + bun transport + genuine tool schemas, while
    the genuine captured opencode prompt passes. "full" is therefore the
    default and "bridge" is documented as gate-dead.
    """
    adapter = ZenDirectAdapter(make_settings())
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    # the genuine opencode prompt asset must be injected verbatim
    assert "interactive CLI tool" in msgs[0]["content"]
    prompt = zen_direct._default_prompt()
    assert msgs[0]["content"].startswith(prompt[:200])
    assert msgs[1] == {"role": "user", "content": "hi"}
    await adapter.aclose()


def test_prompt_asset_is_a_live_client_capture() -> None:
    """The asset must stay a real capture (no hand-written shims): the gate
    rejects anything that does not look like the official client's prompt."""
    prompt = zen_direct._default_prompt()
    assert prompt.startswith("You are opencode, an interactive CLI tool")
    assert "Here is some useful information about the environment" in prompt
    assert "<env>" in prompt and "</env>" in prompt
    # a previous capture leaked the capturing workspace's AGENTS.md appendix
    # into the asset; that is workspace-specific noise, not client text
    assert "Instructions from:" not in prompt
    assert len(prompt) > 5000


def test_render_prompt_binds_model_date_and_workspace() -> None:
    """2026-10-25 live ablation: the body gate compares the prompt's model
    name, date and workspace path against the live request. A frozen capture
    (`Today's date: Sat Oct 24 2026`, model `glm-5.3-flash-free`) gets a
    quota-shaped 429 from an IP that serves the genuine client 200 seconds
    apart; rewriting the date alone flips it to 200."""
    rendered = zen_direct._render_prompt("big-pickle")
    assert "model named big-pickle." in rendered
    assert "opencode/big-pickle" in rendered
    assert "glm-5.3-flash-free" not in rendered
    assert f"Today's date: {zen_direct._prompt_date()}" in rendered
    assert "Working directory: /workspace/opencode" in rendered
    assert "Workspace root folder: /workspace/opencode" in rendered
    # the rest of the capture is untouched
    assert rendered.count("You are powered by the model named") == 1
    assert "Here is some useful information about the environment" in rendered


def test_prompt_date_matches_client_date_string() -> None:
    """Mirror `Date.prototype.toDateString()`: `Sun Oct 25 2026` (UTC)."""
    assert re.fullmatch(
        r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
        r"\d{2} \d{4}",
        zen_direct._prompt_date(),
    )


@pytest.mark.asyncio
async def test_body_marker_renders_requested_model_not_capture_model() -> None:
    """The injected prompt must name the model actually sent upstream; the
    gateway maps public aliases to `-free` ids, so the marker follows it."""
    adapter = ZenDirectAdapter(
        make_settings(zen_marker_mode="full", zen_tools_mode="captured2")
    )
    body = adapter._build_body(
        {"model": "deepseek-v4.1-flash-free",
         "messages": [{"role": "user", "content": "hi"}]},
        "deepseek-v4.1-flash-free",
    )
    marker = body["messages"][0]["content"]
    assert "model named deepseek-v4.1-flash-free." in marker
    assert "opencode/deepseek-v4.1-flash-free" in marker
    assert "big-pickle" not in marker
    await adapter.aclose()



@pytest.mark.asyncio
async def test_zen_model_map_rewrites_public_alias_to_free_id() -> None:
    """Round 5: the free tier only serves `-free` suffixed bare ids; a paid
    bare id answers 401 AuthError "Missing API key" — the exact-map hit must
    win over provider-prefix stripping.
    """
    adapter = ZenDirectAdapter(
        make_settings(
            zen_model_map={
                "deepseek-ai/deepseek-v4.1-flash": "deepseek-v4.1-flash-free",
            }
        )
    )
    assert adapter._zen_model("deepseek-ai/deepseek-v4.1-flash") == (
        "deepseek-v4.1-flash-free"
    )
    # unmapped ids keep the legacy strip-provider behaviour
    assert adapter._zen_model("opencode/big-pickle") == "big-pickle"
    assert adapter._zen_model("big-pickle") == "big-pickle"
    assert adapter._zen_model(None) == "big-pickle"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_tools_mode_all_restores_full_builtin_list() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_tools_mode="all"))
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"bash", "read", "edit", "glob", "grep"} <= names
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_minimal_injection_is_four_tiny_schemas() -> None:
    """Live ablation 2026-10-24: the shape gate tightened to >= 4 distinct
    builtin names; schemas stay un-inspected, so the mandatory fingerprint
    still costs only ~120 prompt tokens with tiny descriptions."""
    adapter = ZenDirectAdapter(make_settings())
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    tools = body["tools"]
    assert len(tools) == 4
    names = {t["function"]["name"] for t in tools}
    assert names == {"bash", "edit", "glob", "read"}
    # tiny: the whole injected set must stay far under the captured ~7.9k chars
    assert len(json.dumps(tools)) < 800
    for t in tools:
        assert t["function"]["parameters"] == {"type": "object", "properties": {}}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_minimal_injection_avoids_client_claimed_names() -> None:
    """Injecting a name the client already uses adds no distinct builtin name
    (the gate counts names) so the pool skips it; the inject count must
    still reach the four-name minimum."""
    adapter = ZenDirectAdapter(make_settings())
    client_tools = [
        {"type": "function", "function": {"name": n, "parameters": {}}}
        for n in ("bash", "read")
    ]
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": client_tools,
        },
        "big-pickle",
    )
    names = [t["function"]["name"] for t in body["tools"]]
    # client tools kept verbatim, plus four injected names that do not collide
    # (pool order: bash*skip, edit, glob, read*skip, todowrite, skill)
    assert names.count("bash") == 1
    assert names.count("read") == 1
    asserted = {n for n in names if n in ("edit", "glob", "todowrite", "skill", "task", "list")}
    assert asserted == {"edit", "glob", "todowrite", "skill"}
    assert len(names) == 6
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_captured2_mode_uses_four_captured_schemas() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_tools_mode="captured2"))
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    tools = body["tools"]
    assert {t["function"]["name"] for t in tools} == {"bash", "edit", "glob", "read"}
    # the captured schemas are far larger than the synthesized ones
    assert len(json.dumps(tools)) > 5000
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_full_mode_injects_opencode_prompt() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_marker_mode="full"))
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    msgs = body["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"].startswith("You are opencode, an interactive CLI tool")
    assert "NEVER call the opencode built-in tools" in msgs[0]["content"]
    assert msgs[1] == {"role": "user", "content": "hi"}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_none_mode_leaves_messages_untouched() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_marker_mode="none"))
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        "big-pickle",
    )
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    # gate requirement stands regardless of marker mode
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"bash", "read"} <= names
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_merges_client_tools_after_builtins() -> None:
    adapter = ZenDirectAdapter(make_settings())
    custom = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": custom,
        },
        "big-pickle",
    )
    names = [t["function"]["name"] for t in body["tools"]]
    # builtin schemas first, client tools appended after
    assert names[0] == "bash"
    assert names[-1] == "get_weather"
    assert "get_weather" in names
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_client_tool_wins_name_collision() -> None:
    adapter = ZenDirectAdapter(make_settings())
    custom = [
        {
            "type": "function",
            "function": {
                "name": "bash",  # collides with a builtin
                "description": "client-side bash",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    body = adapter._build_body(
        {"model": "big-pickle", "messages": [{"role": "user", "content": "x"}], "tools": custom},
        "big-pickle",
    )
    bash_defs = [t for t in body["tools"] if t["function"]["name"] == "bash"]
    assert len(bash_defs) == 1
    assert bash_defs[0]["function"]["description"] == "client-side bash"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_stream_options_mirrors_genuine_client() -> None:
    adapter = ZenDirectAdapter(make_settings())
    body = adapter._build_body(
        {"model": "big-pickle", "stream": True, "messages": [{"role": "user", "content": "x"}]},
        "big-pickle",
    )
    assert body["stream_options"] == {"include_usage": True}
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_no_marker_with_api_key() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [{"role": "user", "content": "hi"}],
        },
        "big-pickle",
    )
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_strips_extra_message_fields() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "internal",
                    "tool_calls": [{"id": "c1", "type": "function"}],
                    "metadata": {"x": 1},
                }
            ],
        },
        "big-pickle",
    )
    msg = body["messages"][0]
    assert msg == {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function"}],
    }
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_drops_turns_with_no_content_or_tool_calls() -> None:
    """Regression: an empty assistant turn (e.g. a reasoning-only message)
    used to be forwarded verbatim and made upstream reject the whole request."""
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    body = adapter._build_body(
        {
            "model": "big-pickle",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            ],
        },
        "big-pickle",
    )
    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}]},
    ]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_body_rejects_when_every_turn_is_empty() -> None:
    adapter = ZenDirectAdapter(make_settings(zen_api_key="oc_sk_test"))
    with pytest.raises(ValueError):
        adapter._build_body(
            {"model": "big-pickle", "messages": [{"role": "assistant", "content": None}]},
            "big-pickle",
        )
    await adapter.aclose()


# --------------------------------------------------------------- chat flow


@respx.mock
@pytest.mark.asyncio
async def test_chat_non_stream_aggregates_sse_and_error_relay() -> None:
    route = respx.post("https://opencode.ai/zen/v1/chat/completions")
    adapter = ZenDirectAdapter(make_settings())

    # Free tier only serves stream=true; non-stream requests are aggregated.
    sse = (
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"role":"assistant","reasoning_content":"thinking"}}]}\n\n'
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"content":"po"}},{"index":0,"delta":{"content":"ng"}}]}\n\n'
        'data: {"id":"z1","created":123,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,'
        '"completion_tokens":2,"total_tokens":12}}\n\n'
        'data: [DONE]\n\n'
        'data: {"choices":[],"cost":"0"}\n\n'
    )
    route.respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=sse.encode(),
    )
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    msg = result["choices"][0]["message"]
    assert msg["content"] == "pong"
    assert msg["reasoning_content"] == "thinking"
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 12
    assert result["id"] == "z1"
    assert result["metadata"]["aggregated_stream"] is True
    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body["model"] == "big-pickle"
    assert body["stream"] is True  # forced upstream
    assert body["stream_options"] == {"include_usage": True}
    assert sent.headers["Authorization"] == "Bearer public"
    assert sent.headers["x-opencode-client"] == "cli"

    route.respond(status_code=429, json={"error": {"message": "Rate limit exceeded."}})
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 429
    await adapter.aclose()


class _StallingResponse:
    """Upstream that sent headers, one SSE line, then went silent."""

    status_code = 200
    headers = httpx.Headers({"content-type": "text/event-stream"})

    async def aiter_lines(self):
        yield 'data: {"id":"z","choices":[{"delta":{"content":"par"}}]}'
        await asyncio.sleep(3600)

    async def aiter_bytes(self):
        async for line in self.aiter_lines():
            yield line.encode()

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_aggregate_stall_raises_a_retryable_connection_error() -> None:
    """A silent body must be a bucket verdict, not a hang.

    Measured 2026-10-27 18:01Z / 18:05Z (ECS, ``stream:false`` clients): the
    bun hop returned response **headers** — so ``forward.ts``'s TTFB timer,
    which only covers headers, never fired — and then delivered no body line.
    Nothing bounded the aggregation, so the request sat on that bucket until
    ``ZEN_FALLBACK_PRIMARY_TIMEOUT_S`` (90s) cancelled it; two clients waited
    96.5s / 97.9s and were then answered by the backup model, while a healthy
    bucket was available the whole time. ``STREAM_STALL_S`` is the bound, and
    the error has to be a ``ConnectionError`` so the failover loop retries.
    """

    with pytest.raises(zen_direct.StreamStalled) as excinfo:
        await zen_direct.aggregate_sse(
            _StallingResponse(), "big-pickle", stall_s=0.05  # type: ignore[arg-type]
        )
    assert isinstance(excinfo.value, ConnectionError)
    assert "stalled" in str(excinfo.value)


@pytest.mark.asyncio
async def test_non_stream_stall_blocks_the_bucket_and_retries_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stall guard must reach the real non-stream chat path, then fail over.

    Asserts the wiring rather than re-testing the retry loop: the stub records
    the ``stall_s`` the adapter passed (it has to be ``STREAM_STALL_S``, not
    the disabled ``0``) and the lease release kind.
    """

    from proxy_opencode.upstreams.zen import client as zen_client

    seen: list[float] = []
    real = zen_client.aggregate_sse

    async def _stub(resp: Any, model: str, *, stall_s: float = 0.0) -> Any:
        seen.append(stall_s)
        if len(seen) == 1:
            raise zen_direct.StreamStalled(
                "zen request failed: aggregate stalled, no body line for 7s"
            )
        return await real(resp, model)

    monkeypatch.setattr(zen_client, "aggregate_sse", _stub)
    client_a, client_b = _leased_client(), _leased_client()
    pool = _RollingStubPool({"bucket-a": client_a, "bucket-b": client_b})
    adapter = ZenDirectAdapter(
        make_settings(stream_stall_s=7.0), pool=pool  # type: ignore[arg-type]
    )
    with respx.mock:
        respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            content=_SSE_OK,
        )
        result = await adapter.chat(
            {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
        )
    assert seen == [7.0, 7.0]
    assert result["choices"][0]["message"]["content"] == "ok"
    assert pool.releases == [
        ("bucket-a", False, "stream_stalled"),
        ("bucket-b", True, ""),
    ]
    await client_a.aclose()
    await client_b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_stream_returns_relay() -> None:
    adapter = ZenDirectAdapter(make_settings())
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
    )
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert isinstance(result, StreamRelay)
    chunks = b"".join([c async for c in result.chunks()])
    assert b"[DONE]" in chunks
    await adapter.aclose()


# ------------------------------------------------- upstream stream drops


def _relay_from_sse(sse: bytes) -> StreamRelay:
    async def gen():
        yield sse

    return StreamRelay(httpx.Response(200, content=gen()), {})


CUT_SSE = (
    b'data: {"id":"z9","created":1,"model":"big-pickle","choices":[{"index":0,'
    b'"delta":{"role":"assistant","content":"partial an"}}]}\n\n'
    b'data: {"id":"z9","created":1,"model":"big-pickle","choices":[{"index":0,'
    b'"delta":{"content":"swer"}}]}\n\n'
)


@pytest.mark.asyncio
async def test_relay_synthesizes_finish_chunk_on_silent_cut() -> None:
    # Live-verified 2026-10-13: Zen providers intermittently close streams
    # mid-generation with no finish chunk, no usage chunk and no [DONE].
    relay = _relay_from_sse(CUT_SSE)
    out = b"".join([c async for c in relay.chunks()])
    assert relay.stream_cut is True
    assert out.startswith(CUT_SSE)  # original bytes relayed verbatim
    assert b'"finish_reason":"length"' in out
    assert out.rstrip().endswith(b"data: [DONE]")
    # the synthesized chunk reuses the upstream completion id
    assert b'"id":"z9"' in out[len(CUT_SSE):]


@pytest.mark.asyncio
async def test_relay_untouched_when_stream_completes_normally() -> None:
    sse = (
        b'data: {"id":"z1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        b'data: {"id":"z1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"id":"z1","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        b'data: [DONE]\n\n'
    )
    relay = _relay_from_sse(sse)
    out = b"".join([c async for c in relay.chunks()])
    assert out == sse
    assert relay.stream_cut is False
    assert relay.usage_holder["usage"]["completion_tokens"] == 1


@pytest.mark.asyncio
async def test_relay_appends_done_when_only_finish_present() -> None:
    sse = (
        b'data: {"id":"z2","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
        b'data: {"id":"z2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    relay = _relay_from_sse(sse)
    out = b"".join([c async for c in relay.chunks()])
    assert relay.stream_cut is False
    assert out == sse + b"data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_relay_cut_split_across_tcp_chunks_still_detected() -> None:
    # the finish-less cut must be detected even when SSE lines straddle
    # arbitrary TCP chunk boundaries

    async def gen():
        raw = CUT_SSE
        for i in range(0, len(raw), 7):
            yield raw[i : i + 7]

    relay = StreamRelay(httpx.Response(200, content=gen()), {})
    out = b"".join([c async for c in relay.chunks()])
    assert relay.stream_cut is True
    assert out.startswith(CUT_SSE)
    assert b'"finish_reason":"length"' in out
    assert out.rstrip().endswith(b"data: [DONE]")


@respx.mock
@pytest.mark.asyncio
async def test_chat_non_stream_cut_is_reported_as_length() -> None:
    route = respx.post("https://opencode.ai/zen/v1/chat/completions")
    adapter = ZenDirectAdapter(make_settings())
    sse = (
        'data: {"id":"z8","created":9,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"content":"half a"}}]}\n\n'
        'data: {"id":"z8","created":9,"model":"big-pickle","choices":[{"index":0,'
        '"delta":{"content":"n answer"}}]}\n\n'
    )
    route.respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=sse.encode(),
    )
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["choices"][0]["message"]["content"] == "half an answer"
    assert result["metadata"]["upstream_stream_cut"] is True
    await adapter.aclose()


# ---------------------------------------------------- stream stall watchdog


@pytest.mark.asyncio
async def test_relay_stall_watchdog_synthesises_terminator() -> None:
    # 2026-10-19 incident: a dead egress tunnel left the stream silent until
    # the 300s read timeout; httpx.ReadTimeout then tore the client
    # connection down with no terminator (hermes: "The connection dropped
    # before the reply finished."). The watchdog must stop waiting first and
    # close the wire cleanly via the synthesised-terminator path.
    async def gen():
        yield (
            b'data: {"id":"z7","choices":[{"index":0,'
            b'"delta":{"content":"part"}}]}\n\n'
        )
        await asyncio.sleep(60)  # tunnel hangs mid-stream
        raise AssertionError("stall watchdog never fired")  # pragma: no cover

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, stall_s=0.2)
    out = b"".join([c async for c in relay.chunks()])
    assert relay.end_reason == "stalled"
    assert relay.stream_cut is True
    assert out.startswith(b'data: {"id":"z7"')  # pre-stall bytes verbatim
    assert b'"finish_reason":"length"' in out
    assert out.rstrip().endswith(b"data: [DONE]")


@pytest.mark.asyncio
async def test_relay_stall_watchdog_covers_first_byte() -> None:
    # Response headers arrived but the first body byte never does: the
    # watchdog window covers that gap too, so a silent tunnel is bounded by
    # stall_s instead of the full read timeout.
    async def gen():
        await asyncio.sleep(60)
        yield b"unreachable"  # pragma: no cover

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, stall_s=0.2)
    out = b"".join([c async for c in relay.chunks()])
    assert relay.end_reason == "stalled"
    assert relay.stream_cut is True
    assert b'"finish_reason":"length"' in out
    assert out.rstrip().endswith(b"data: [DONE]")


@pytest.mark.asyncio
async def test_relay_watchdog_passes_through_normal_completion() -> None:
    # End-of-stream (StopAsyncIteration) must flow through the wait_for
    # wrapper untouched when the watchdog is armed.
    sse = (
        b'data: {"id":"z1","choices":[{"index":0,"delta":{"content":"hi"}},'
        b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )

    async def gen():
        yield sse

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, stall_s=5.0)
    out = b"".join([c async for c in relay.chunks()])
    assert out == sse
    assert relay.end_reason == "completed"
    assert relay.stream_cut is False


@pytest.mark.asyncio
async def test_relay_interrupted_mid_stream_synthesises_terminator() -> None:
    async def gen():
        yield (
            b'data: {"id":"z6","choices":[{"index":0,'
            b'"delta":{"content":"pa"}}]}\n\n'
        )
        raise httpx.ReadError("connection reset by peer")

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, stall_s=5.0)
    out = b"".join([c async for c in relay.chunks()])
    assert relay.end_reason == "interrupted"
    assert relay.stream_cut is True
    assert b'"finish_reason":"length"' in out
    assert out.rstrip().endswith(b"data: [DONE]")


@pytest.mark.asyncio
async def test_relay_close_hook_reports_stall_outcome() -> None:
    calls: list[str] = []

    async def hook(outcome: str) -> None:
        calls.append(outcome)

    async def gen():
        await asyncio.sleep(60)
        yield b"unreachable"  # pragma: no cover

    relay = StreamRelay(
        httpx.Response(200, content=gen()), {}, on_close=hook, stall_s=0.2
    )
    _ = b"".join([c async for c in relay.chunks()])
    assert calls == ["stalled"]


@respx.mock
@pytest.mark.asyncio
async def test_chat_stalled_stream_releases_lease_as_failure() -> None:
    # End-to-end through the pool: a tunnel that stops delivering must
    # release the lease as a FAILURE (kind=stream_stalled) so three
    # consecutive stalls cordon the dead bucket instead of it never
    # accumulating a single failure (the 2026-10-19 diagnosis: release was
    # hardcoded ok=True and bad buckets were never cordoned).
    async def body():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        await asyncio.sleep(60)  # egress tunnel hangs
        raise AssertionError("stall watchdog never fired")  # pragma: no cover

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=body(),
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(
        make_settings(stream_stall_s=0.2), pool=pool  # type: ignore[arg-type]
    )
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert isinstance(result, StreamRelay)
    assert pool.releases == []  # lease held while the stream is live
    out = b"".join([c async for c in result.chunks()])
    assert b'"finish_reason":"length"' in out  # clean wire, not a hard drop
    assert pool.releases == [("bucket-a", False, "stream_stalled")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_models_endpoint() -> None:
    adapter = ZenDirectAdapter(make_settings())
    respx.get("https://opencode.ai/zen/v1/models").respond(
        json={"data": [{"id": "big-pickle"}, {"id": "qwen3-coder"}]}
    )
    result = await adapter.list_models()
    ids = [m["id"] for m in result["data"]]
    assert "opencode/big-pickle" in ids
    assert "opencode/qwen3-coder" in ids
    assert result["metadata"]["adapter"] == "zen-direct"
    await adapter.aclose()


@pytest.mark.asyncio
async def test_rejects_non_zen_host() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        ZenDirectAdapter(make_settings(zen_base_url="https://evil.example.com/v1"))


def test_settings_roundtrip() -> None:
    import os

    os.environ["UPSTREAM_MODE"] = "zen-direct"
    os.environ["OPENCODE_ZEN_API_KEY"] = "oc_sk_x"
    os.environ["ZEN_PROXY"] = "http://127.0.0.1:7897"
    try:
        from proxy_opencode.config import load_settings

        s = load_settings()
        assert s.is_zen_direct
        assert s.zen_api_key == "oc_sk_x"
        assert s.zen_proxy == "http://127.0.0.1:7897"
    finally:
        del os.environ["UPSTREAM_MODE"]
        del os.environ["OPENCODE_ZEN_API_KEY"]
        del os.environ["ZEN_PROXY"]


def test_httpx_proxy_acceptance() -> None:
    # httpx accepts the proxy form we document; keeps config honest.
    client = httpx.Client(proxy="http://127.0.0.1:7897")
    client.close()


# ------------------------------------------------------- egress pool leases


class _StubPool:
    """Minimal EgressPool stand-in recording releases (0.7.x integration)."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self.releases: list[tuple[str, bool, str]] = []
        # Estimated prompt tokens the adapter stamped on each lease; the
        # bucket's daily token budget is charged from exactly this number.
        self.lease_tokens: list[int] = []

    @property
    def size(self) -> int:
        return 1

    async def acquire(self) -> Lease:
        state = EgressState(spec=EgressSpec("bucket-a", "http://agent:10001"))
        state.client = self._client
        state.inflight += 1
        return Lease(state)

    async def release(
        self,
        lease: Lease,
        *,
        ok: bool,
        kind: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        self._retry_after = retry_after_s
        self.lease_tokens.append(lease.tokens)
        self.releases.append((lease.name, ok, kind))


def _leased_client() -> httpx.AsyncClient:
    # The real pool builds per-egress clients WITHOUT a base_url (they exist
    # only to route through a device proxy), so upstream paths must be
    # absolute. Keep this client base_url-less to guard that invariant.
    return httpx.AsyncClient()


@respx.mock
@pytest.mark.asyncio
async def test_chat_stream_leases_egress_and_releases_on_stream_end() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert isinstance(result, StreamRelay)
    # the lease must stay held while the stream is live
    assert pool.releases == []
    _ = b"".join([c async for c in result.chunks()])
    # no finish_reason upstream -> clean cut, still an ok release (the
    # healthy tunnel merely forwarded upstream's truncation) but with the
    # outcome recorded for bucket stats
    assert pool.releases == [("bucket-a", True, "stream_cut")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_stamps_the_prompt_estimate_on_every_lease() -> None:
    """The token budget is metered from the estimate the guard already made.

    Nothing else in the request path knows the prompt size before the
    upstream answers, and the upstream bills tokens rather than calls
    (2026-10-28: 166 calls == one exhausted IP). If the stamp goes missing
    the budget silently never fires, so this wiring is pinned here.
    """

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    prompt = "the quick brown fox jumps over the lazy dog " * 40
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
    )
    assert isinstance(result, StreamRelay)
    _ = b"".join([c async for c in result.chunks()])
    assert len(pool.lease_tokens) == 1
    # ~500 tokens of prose plus the injected marker prompt and builtin tools.
    assert pool.lease_tokens[0] > 300
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_stream_releases_lease_on_client_disconnect() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"b"}}]}\n\ndata: [DONE]\n\n',
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {
            "model": "opencode/big-pickle",
            "stream": True,
            "messages": [{"role": "user", "content": "q"}],
        }
    )
    assert isinstance(result, StreamRelay)
    gen = result.chunks()
    await gen.__anext__()  # consume one chunk, then hang up
    await gen.aclose()
    assert pool.releases == [("bucket-a", True, "client_disconnected")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_non_stream_releases_lease_ok() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=200,
        headers={"content-type": "text/event-stream"},
        content=(
            b'data: {"id":"z","choices":[{"delta":{"content":"hi"},'
            b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
        ),
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["message"]["content"] == "hi"
    assert pool.releases == [("bucket-a", True, "")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_429_cordons_the_egress() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=429, json={"error": {"message": "FreeUsageLimitError"}}
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 429
    assert pool.releases == [("bucket-a", False, "http_429")]
    await client.aclose()
    await adapter.aclose()


# ------------------------------------------------------- bucket failover


class _RollingStubPool:
    """N-bucket stand-in: acquire() walks the buckets in a fixed order."""

    def __init__(self, buckets: dict[str, httpx.AsyncClient]) -> None:
        self._names = list(buckets)
        self._clients = buckets
        self._i = 0
        self.releases: list[tuple[str, bool, str]] = []

    @property
    def size(self) -> int:
        return len(self._names)

    async def acquire(self) -> Lease:
        name = self._names[self._i % len(self._names)]
        self._i += 1
        state = EgressState(spec=EgressSpec(name, "http://agent:10001"))
        state.client = self._clients[name]
        state.inflight += 1
        return Lease(state)

    async def release(
        self,
        lease: Lease,
        *,
        ok: bool,
        kind: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        self.releases.append((lease.name, ok, kind))


_SSE_OK = (
    b'data: {"id":"z","choices":[{"delta":{"content":"ok"},'
    b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
)


@respx.mock
@pytest.mark.asyncio
async def test_chat_429_fails_over_to_next_bucket() -> None:
    """A quota 429 must not surface to the client while another bucket lives.

    Measured 2026-10-17: the only remaining bucket drained its quota and
    the error rode through verbatim. The lease is released and the same
    body retried on the next bucket; client sees the aggregated 200.
    """

    seq = {"n": 0}

    def _upstream(_request: httpx.Request) -> httpx.Response:
        if seq["n"] == 0:
            seq["n"] += 1
            return httpx.Response(
                429, json={"error": {"message": "FreeUsageLimitError"}}
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
        )

    respx.post("https://opencode.ai/zen/v1/chat/completions").mock(
        side_effect=_upstream
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert pool.releases == [
        ("bucket-a", False, "http_429"),
        ("bucket-b", True, ""),
    ]
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_mints_fresh_session_per_upstream_attempt() -> None:
    """Upstream sessions are single-use (live 2026-10-24): replaying a
    consumed session returns the quota-shaped 429 even on a healthy egress
    IP with byte-identical headers/body. Headers must therefore be minted
    per upstream attempt — including every failover leg — instead of once
    per client request.
    """

    seq = {"n": 0}
    sessions: list[str] = []

    def _upstream(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        if seq["n"] == 0:
            seq["n"] += 1
            return httpx.Response(
                429, json={"error": {"message": "FreeUsageLimitError"}}
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
        )

    respx.post("https://opencode.ai/zen/v1/chat/completions").mock(
        side_effect=_upstream
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert len(sessions) == 2
    assert len({sessions[0], sessions[1]}) == 2
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_429_surfaces_after_failover_budget_is_exhausted() -> None:
    """Every bucket 429ing must return the last upstream 429 (bounded)."""

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=429, json={"error": {"message": "FreeUsageLimitError"}}
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 429
    assert pool.releases == [
        ("bucket-a", False, "http_429"),
        ("bucket-b", False, "http_429"),
    ]
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_transport_failure_fails_over_to_next_bucket() -> None:
    seq = {"n": 0}

    def _upstream(request: httpx.Request) -> httpx.Response:
        if seq["n"] == 0:
            seq["n"] += 1
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
        )

    respx.post("https://opencode.ai/zen/v1/chat/completions").mock(
        side_effect=_upstream
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert pool.releases == [
        ("bucket-a", False, "transport"),
        ("bucket-b", True, ""),
    ]
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_400_is_not_an_egress_failure() -> None:
    # A request-level rejection rode through a working egress: release it ok.
    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=400, json={"error": {"message": "bad request"}}
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 400
    assert pool.releases == [("bucket-a", True, "")]
    await client.aclose()
    await adapter.aclose()


# ------------------------------------- free-tier 403 (shape/egress defensive)
# 2026-10-17 measured egress-style failover; 2026-10-24 re-derived these as
# the shape gate (>= 4 real builtin names) with the cordon kept defensive.
# The behaviour contract below is unchanged.

_FREE_TIER_BODY = {
    "type": "error",
    "error": {
        "type": "FreeTierError",
        "message": "OpenCode's free tier can only be used from within OpenCode.",
    },
}


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_free_tier_cordons_the_egress() -> None:
    """A 403 FreeTierError counts against the bucket, unlike other 4xx.

    Measured 2026-10-17: the gate is decided by the egress IP — a
    byte-identical body 200s from a fresh IP and 403s from a flagged one.
    Single-bucket pool: there is nowhere left to fail over, so the client
    sees the mapped retryable 503 instead of the relayed 403.
    """

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=403, json=_FREE_TIER_BODY
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 503
    assert result["content"]["error"]["code"] == "egress_unavailable"
    assert pool.releases == [("bucket-a", False, "free_tier_rejected")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_free_tier_fails_over_to_next_bucket() -> None:
    """The flagged IP must not surface to the client while another lives."""

    seq = {"n": 0}

    def _upstream(_request: httpx.Request) -> httpx.Response:
        if seq["n"] == 0:
            seq["n"] += 1
            return httpx.Response(403, json=_FREE_TIER_BODY)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_SSE_OK
        )

    respx.post("https://opencode.ai/zen/v1/chat/completions").mock(
        side_effect=_upstream
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["choices"][0]["message"]["content"] == "ok"
    assert pool.releases == [
        ("bucket-a", False, "free_tier_rejected"),
        ("bucket-b", True, ""),
    ]
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_free_tier_answers_503_after_failover_budget_exhausted() -> None:
    """Every bucket flagged → a bounded 503, not a client-blaming 403.

    2026-10-19 06:39Z: two flagged IPs plus one dead tunnel spent all three
    attempts and the relayed 403 reached hermes as an auth error with
    retryable=false. The egress-level rejection must surface as a retryable
    503 `egress_unavailable` instead; releases still cordon both buckets.
    """

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=403, json=_FREE_TIER_BODY
    )
    a, b = httpx.AsyncClient(), httpx.AsyncClient()
    pool = _RollingStubPool({"bucket-a": a, "bucket-b": b})
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 503
    error = result["content"]["error"]
    assert error["type"] == "api_error"
    assert error["code"] == "egress_unavailable"
    assert pool.releases == [
        ("bucket-a", False, "free_tier_rejected"),
        ("bucket-b", False, "free_tier_rejected"),
    ]
    await a.aclose()
    await b.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_free_tier_without_pool_answers_503() -> None:
    """No pool means no failover, but the rejection is still egress-level.

    The mapped 503 must not depend on failover machinery: a relayed 403
    would still blame the client for the gateway's flagged egress IP.
    """

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=403, json=_FREE_TIER_BODY
    )
    adapter = ZenDirectAdapter(make_settings())
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 503
    assert result["content"]["error"]["code"] == "egress_unavailable"
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_other_error_type_is_not_an_egress_failure() -> None:
    """Only the free-tier egress gate counts against the bucket.

    A 403 carrying a different error type is a final request-level answer
    through a working egress: release it ok and do not fail over.
    """

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=403,
        json={"error": {"type": "AuthError", "message": "Missing API key"}},
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 403
    assert pool.releases == [("bucket-a", True, "")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_403_free_tier_with_api_key_is_not_an_egress_failure() -> None:
    """Keyed mode: FreeTierError is not an egress signal (the pool is not free)."""

    respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=403, json=_FREE_TIER_BODY
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(
        make_settings(zen_api_key="oc_sk_test"), pool=pool
    )  # type: ignore[arg-type]
    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )
    assert result["__status__"] == 403
    assert pool.releases == [("bucket-a", True, "")]
    await client.aclose()
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_chat_transport_error_releases_lease_as_failure() -> None:
    respx.post("https://opencode.ai/zen/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("boom")
    )
    client = _leased_client()
    pool = _StubPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]
    with pytest.raises(ConnectionError):
        await adapter.chat(
            {
                "model": "opencode/big-pickle",
                "messages": [{"role": "user", "content": "q"}],
            }
        )
    assert pool.releases == [("bucket-a", False, "transport")]
    await client.aclose()
    await adapter.aclose()


# ------------------------------------------------------- relay close hook


@pytest.mark.asyncio
async def test_relay_on_close_fires_once_after_full_stream() -> None:
    calls: list[str] = []

    async def hook(outcome: str) -> None:
        calls.append(outcome)

    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, on_close=hook)
    out = b"".join([c async for c in relay.chunks()])
    assert b"[DONE]" in out
    # no finish_reason chunk upstream -> clean cut, reported as such
    assert calls == ["cut"]


@pytest.mark.asyncio
async def test_relay_on_close_fires_on_early_aclose() -> None:
    calls: list[str] = []

    async def hook(outcome: str) -> None:
        calls.append(outcome)

    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, on_close=hook)
    it = relay.chunks()
    await it.__anext__()
    await it.aclose()
    # closed by the consumer before the stream ended -> client disconnect
    assert calls == ["disconnected"]


@pytest.mark.asyncio
async def test_relay_on_close_outcome_completed_when_finish_seen() -> None:
    calls: list[str] = []

    async def hook(outcome: str) -> None:
        calls.append(outcome)

    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"hi"},' \
            b'"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    relay = StreamRelay(httpx.Response(200, content=gen()), {}, on_close=hook)
    _ = b"".join([c async for c in relay.chunks()])
    assert calls == ["completed"]


@pytest.mark.asyncio
async def test_chat_answers_quota_blackout_without_touching_upstream() -> None:
    """When every bucket is out of quota, skip the upstream entirely.

    The allowance is account-scoped (verified 2026-10-29 with a freshly
    rebooted MiFi IP), so walking the benched pool only added one upstream
    round trip per bucket before the fallback chain took over.
    """

    class _BlackoutPool(_StubPool):
        called = 0

        def quota_blackout_s(self) -> float:
            type(self).called += 1
            return 20893.0

    client = _leased_client()
    pool = _BlackoutPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://opencode.ai/zen/v1/chat/completions")
        result = await adapter.chat(
            {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
        )
        assert route.call_count == 0, "no upstream attempt during a blackout"

    assert result["__status__"] == 429
    assert result["__quota_blackout_s__"] == 20893.0
    assert result["content"]["error"]["code"] == "free_quota_exhausted"


@respx.mock
@pytest.mark.asyncio
async def test_chat_stops_retrying_once_a_429_tips_the_pool_into_blackout() -> None:
    """The first request of an exhausted day must stop at the tipping 429.

    Before this, a fresh process (cordon state empty) walked every bucket —
    four upstream round trips, 7.14s measured — before the fallback chain
    could answer a 16-token prompt.
    """

    class _TippingPool(_StubPool):
        @property
        def size(self) -> int:
            return 4  # four benchable buckets in this scenario

        def __init__(self, client: httpx.AsyncClient) -> None:
            super().__init__(client)
            self.probes = 0

        def quota_blackout_s(self) -> float:
            self.probes += 1
            # 0 while the pre-loop check runs, then "everything is benched".
            return 0.0 if self.probes == 1 else 20893.0

    route = respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=429,
        headers={"retry-after": "20893"},
        json={"error": {"type": "FreeUsageLimitError", "message": "limit"}},
    )
    client = _leased_client()
    pool = _TippingPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]

    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )

    assert route.call_count == 1, "must not walk the rest of the dead pool"
    assert result["__status__"] == 429
    assert result["__quota_blackout_s__"] == 20893.0


@respx.mock
@pytest.mark.asyncio
async def test_final_429_retry_after_becomes_the_blackout_deadline() -> None:
    """With 3 attempts for 4 buckets the pool never looks fully benched.

    The last 429 still carries the definitive evidence — Retry-After beyond the
    throttle boundary — so the fallback chain gets the reset deadline instead
    of the flat hold, on the very first request of an exhausted day.
    """

    class _NeverBlackoutPool(_StubPool):
        @property
        def size(self) -> int:
            return 4

        def quota_blackout_s(self) -> float:
            return 0.0

    route = respx.post("https://opencode.ai/zen/v1/chat/completions").respond(
        status_code=429,
        headers={"retry-after": "20893"},
        json={"error": {"type": "FreeUsageLimitError", "message": "limit"}},
    )
    client = _leased_client()
    pool = _NeverBlackoutPool(client)
    adapter = ZenDirectAdapter(make_settings(), pool=pool)  # type: ignore[arg-type]

    result = await adapter.chat(
        {"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "q"}]}
    )

    assert route.call_count == 3, "attempt budget still caps the walk"
    assert result["__status__"] == 429
    assert result["__quota_blackout_s__"] == 20893.0
