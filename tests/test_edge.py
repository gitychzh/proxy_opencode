"""Edge tests: the two guarantees that matter.

1. A healthy primary is a **byte-transparent** hop (status, headers, streaming
   body untouched) — the edge must never become a second gateway.
2. A dead primary is **invisible** to the client: the request is replayed on the
   backup relay with its own credentials and model id, and the client still sees
   the model it asked for.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from proxy_opencode.edge.app import EdgeApp
from proxy_opencode.edge.config import EdgeSettings, load_edge_settings
from proxy_opencode.edge.policy import (
    failover_reason,
    filter_request_headers,
    filter_response_headers,
    mask_model_in_json,
    quota_verdict,
    rewrite_for_fallback,
)

PRIMARY = "http://primary.test"
FALLBACK = "http://fallback.test"
CHAT = "/v1/chat/completions"


def make_settings(**over: Any) -> EdgeSettings:
    base: dict[str, Any] = {
        "primary_base_url": PRIMARY,
        "fallback_base_url": FALLBACK,
        "fallback_api_key": "ms-key",
        "fallback_model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "admin_key": "edge-admin",
    }
    base.update(over)
    return EdgeSettings(**base)


class Harness:
    """Minimal ASGI driver: one request in, all emitted messages out."""

    def __init__(self, app: EdgeApp) -> None:
        self.app = app

    async def call(
        self,
        *,
        method: str = "POST",
        path: str = CHAT,
        body: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        if isinstance(body, dict):
            raw = json.dumps(body).encode()
            default = {"content-type": "application/json"}
        else:
            raw = body or b""
            default = {}
        merged = {**default, **(headers or {})}
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in merged.items()],
        }
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        await self.app(scope, receive, send)

        start = next(m for m in sent if m["type"] == "http.response.start")
        head = {k.decode(): v.decode() for k, v in start["headers"]}
        payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], head, payload


# --------------------------------------------------------------------- policy


def test_request_headers_drop_framing_and_force_identity_encoding() -> None:
    kept = dict(
        filter_request_headers(
            [
                (b"host", b"127.0.0.1:10002"),
                (b"content-length", b"42"),
                (b"connection", b"keep-alive"),
                (b"accept-encoding", b"gzip, br"),
                (b"authorization", b"Bearer api_x"),
                (b"content-type", b"application/json"),
            ]
        )
    )
    assert "host" not in kept and "content-length" not in kept
    assert "connection" not in kept
    assert kept["accept-encoding"] == "identity"
    assert kept["authorization"] == "Bearer api_x"
    assert kept["content-type"] == "application/json"


def test_response_headers_drop_length_so_chunking_is_ours() -> None:
    kept = dict(
        filter_response_headers(
            [(b"content-length", b"9"), (b"content-type", b"text/event-stream")]
        )
    )
    assert "content-length" not in kept
    assert kept["content-type"] == "text/event-stream"


def test_quota_verdict_needs_a_daily_horizon_not_a_short_throttle() -> None:
    assert quota_verdict(429, [(b"retry-after", b"20893")], b"")
    assert not quota_verdict(429, [(b"retry-after", b"30")], b"")
    body = json.dumps({"error": {"code": "free_quota_exhausted"}}).encode()
    assert quota_verdict(429, [], body)
    assert not quota_verdict(400, [(b"retry-after", b"99999")], b"")


def test_failover_reason_only_covers_outages() -> None:
    kwargs = {"failover_statuses": frozenset({502, 503, 504}), "failover_on_quota": True}
    assert failover_reason(503, [], b"", **kwargs) == "primary_http_503"
    assert failover_reason(502, [], b"", **kwargs) == "primary_http_502"
    # Client errors are answers, not outages.
    assert failover_reason(400, [], b"{}", **kwargs) is None
    assert failover_reason(401, [], b"{}", **kwargs) is None
    assert failover_reason(404, [], b"{}", **kwargs) is None
    # A quota verdict is an outage the backup can cover.
    assert failover_reason(429, [(b"retry-after", b"20000")], b"", **kwargs) == (
        "primary_quota_exhausted"
    )
    off = {**kwargs, "failover_on_quota": False}
    assert failover_reason(429, [(b"retry-after", b"20000")], b"", **off) is None


def test_rewrite_points_the_body_at_the_backup_model() -> None:
    settings = make_settings()
    body = json.dumps({"model": "opencode/big-pickle", "messages": []}).encode()
    rewritten = rewrite_for_fallback(body, settings)
    assert rewritten is not None
    new_body, original = rewritten
    assert original == "opencode/big-pickle"
    assert json.loads(new_body)["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    # Non-JSON bodies cannot be rewritten, so no fallback is attempted for them.
    assert rewrite_for_fallback(b"<xml/>", settings) is None


def test_mask_model_keeps_the_clients_own_id() -> None:
    body = json.dumps({"model": "deepseek-ai/DeepSeek-V4.1-Flash-1017", "id": "x"}).encode()
    masked = json.loads(mask_model_in_json(body, "opencode/big-pickle"))
    assert masked["model"] == "opencode/big-pickle"
    assert mask_model_in_json(b"not json", "m") == b"not json"


def test_edge_settings_validate_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_MODE", "nonsense")
    with pytest.raises(ValueError):
        load_edge_settings()
    monkeypatch.setenv("EDGE_MODE", "fallback")
    assert load_edge_settings().mode == "fallback"


# ----------------------------------------------------------------- integration


@respx.mock
async def test_healthy_primary_is_transparent() -> None:
    route = respx.post(f"{PRIMARY}{CHAT}").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-gateway-fallback": "none"},
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n',
        )
    )
    app = EdgeApp(make_settings())
    status, head, body = await Harness(app).call(
        body={"model": "opencode/big-pickle", "stream": True, "messages": []}
    )

    assert status == 200
    assert head["x-edge-route"] == "primary"
    assert head["content-type"] == "text/event-stream"
    assert head["x-gateway-fallback"] == "none"
    assert b"[DONE]" in body
    assert route.call_count == 1
    await app._shutdown()


@respx.mock
async def test_gateway_down_is_served_by_the_backup_relay() -> None:
    """The redeploy case: primary refuses connections, users see no error."""

    respx.post(f"{PRIMARY}{CHAT}").mock(side_effect=httpx.ConnectError("refused"))
    fallback = respx.post(f"{FALLBACK}{CHAT}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )
    )
    app = EdgeApp(make_settings())
    status, head, body = await Harness(app).call(
        body={"model": "opencode/big-pickle", "messages": [{"role": "user", "content": "hi"}]},
        headers={"authorization": "Bearer api_client"},
    )

    assert status == 200
    assert head["x-edge-route"] == "fallback"
    assert head["x-edge-fallback-reason"].startswith("ConnectError")
    # The client's model id is echoed back, the backup's own credentials are used.
    payload = json.loads(body)
    assert payload["model"] == "opencode/big-pickle"
    sent = fallback.calls[0].request
    assert sent.headers["authorization"] == "Bearer ms-key"
    assert json.loads(sent.content)["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    await app._shutdown()


@respx.mock
async def test_primary_503_fails_over_on_the_same_request() -> None:
    respx.post(f"{PRIMARY}{CHAT}").mock(
        return_value=httpx.Response(
            503, json={"error": {"code": "egress_unavailable", "message": "no egress"}}
        )
    )
    respx.post(f"{FALLBACK}{CHAT}").mock(
        return_value=httpx.Response(200, json={"model": "backup", "choices": []})
    )
    app = EdgeApp(make_settings())
    status, head, _ = await Harness(app).call(
        body={"model": "deepseek-ai/deepseek-v4.1-flash", "messages": []}
    )

    assert status == 200
    assert head["x-edge-route"] == "fallback"
    assert head["x-edge-fallback-reason"] == "primary_http_503"
    await app._shutdown()


@respx.mock
async def test_client_errors_are_never_covered_up() -> None:
    respx.post(f"{PRIMARY}{CHAT}").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": "invalid api key", "type": "authentication_error"}}
        )
    )
    fallback = respx.post(f"{FALLBACK}{CHAT}")
    app = EdgeApp(make_settings())
    status, head, body = await Harness(app).call(
        body={"model": "m", "messages": []}, headers={"authorization": "Bearer nope"}
    )

    assert status == 401
    assert head["x-edge-route"] == "primary"
    assert json.loads(body)["error"]["type"] == "authentication_error"
    assert fallback.call_count == 0
    await app._shutdown()


@respx.mock
async def test_quota_429_hands_the_turn_to_the_backup() -> None:
    """An account-level quota verdict would fail for hours; the backup serves."""

    respx.post(f"{PRIMARY}{CHAT}").mock(
        return_value=httpx.Response(
            429,
            headers={"retry-after": "20893"},
            json={"error": {"code": "free_quota_exhausted", "message": "quota"}},
        )
    )
    respx.post(f"{FALLBACK}{CHAT}").mock(
        return_value=httpx.Response(200, json={"model": "backup", "choices": []})
    )
    app = EdgeApp(make_settings())
    status, head, _ = await Harness(app).call(body={"model": "m", "messages": []})

    assert status == 200
    assert head["x-edge-fallback-reason"] == "primary_quota_exhausted"
    await app._shutdown()


@respx.mock
async def test_non_chat_paths_keep_the_primary_error() -> None:
    """No silent format conversion: /v1/messages cannot be replayed on the backup."""

    respx.post(f"{PRIMARY}/v1/messages").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    fallback = respx.post(f"{FALLBACK}/v1/messages")
    app = EdgeApp(make_settings())
    status, head, _ = await Harness(app).call(
        path="/v1/messages", body={"model": "m", "messages": []}
    )

    assert status == 503
    assert head["x-edge-route"] == "primary"
    assert fallback.call_count == 0
    await app._shutdown()


@respx.mock
async def test_forced_fallback_mode_drains_the_primary() -> None:
    """OPS: drain the primary before a rebuild without touching the port."""

    primary = respx.post(f"{PRIMARY}{CHAT}")
    respx.post(f"{FALLBACK}{CHAT}").mock(
        return_value=httpx.Response(200, json={"model": "backup", "choices": []})
    )
    app = EdgeApp(make_settings(mode="fallback"))
    status, head, _ = await Harness(app).call(body={"model": "m", "messages": []})

    assert status == 200
    assert head["x-edge-route"] == "fallback"
    assert primary.call_count == 0
    await app._shutdown()


async def test_healthz_reports_both_hops_and_hides_admin_without_key() -> None:
    app = EdgeApp(make_settings())
    status, _, body = await Harness(app).call(method="GET", path="/healthz")
    state = json.loads(body)
    assert status == 200
    assert state["role"] == "edge"
    assert state["primary"]["base"] == PRIMARY
    assert state["fallback"]["model"] == "deepseek-ai/DeepSeek-V4.1-Flash-1017"

    # Admin surface is invisible unless EDGE_ADMIN_KEY is set.
    locked = EdgeApp(make_settings(admin_key=""))
    status, _, _ = await Harness(locked).call(method="GET", path="/edge/status")
    assert status == 404
    status, _, _ = await Harness(app).call(method="GET", path="/edge/status")
    assert status == 404  # no key supplied
    status, _, body = await Harness(app).call(
        method="GET", path="/edge/status", headers={"x-admin-key": "edge-admin"}
    )
    assert status == 200 and json.loads(body)["role"] == "edge"
    await app._shutdown()
    await locked._shutdown()


async def test_admin_can_switch_modes_at_runtime() -> None:
    app = EdgeApp(make_settings())
    status, _, body = await Harness(app).call(
        method="POST",
        path="/edge/mode",
        body={"mode": "fallback"},
        headers={"x-admin-key": "edge-admin"},
    )
    assert status == 200
    assert json.loads(body)["mode"] == "fallback"
    assert app._preferred_route() == "fallback"  # noqa: SLF001 - assertion seam

    status, _, body = await Harness(app).call(
        method="POST",
        path="/edge/mode",
        body={"mode": "bogus"},
        headers={"x-admin-key": "edge-admin"},
    )
    assert status == 200 and "error" in json.loads(body)
    await app._shutdown()
