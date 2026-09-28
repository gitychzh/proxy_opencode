"""Unit tests for the two-bucket zen_lb balancer.

The balancer's real job is dispatching to two DIFFERENT upstreams with two
different bearer keys, so the tests mount two in-process fake gateways and
route the balancer's outbound client to them by hostname. A single
ASGITransport cannot express that (it would call the balancer itself), hence
the custom transport below.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ZEN_LB_API_KEY"] = "lb-secret"
os.environ["ZEN_LB_HEALTH_INTERVAL"] = "3600"

import httpx  # noqa: E402
import pytest  # noqa: E402

# Local module: must be imported AFTER the env vars above are set, because lb
# reads ZEN_LB_API_KEY / ZEN_LB_HEALTH_INTERVAL at import time. isort would
# otherwise hoist it next to the third-party imports.
import lb  # noqa: E402  # isort:skip

SEEN: list[tuple[str, str, str]] = []
RID_SEEN: list[str] = []
ROUTES: dict[str, object] = {}
DEAD: set[str] = set()
SLOW: dict[str, float] = {}
# 桶对请求回 429：QUOTA_429 模拟 Zen 免费层额度耗尽（FreeUsageLimitError），
# OWN_429 模拟桶网关自己的限流（code=rate_limit_exceeded）。只有前者该熔断。
QUOTA_429: set[str] = set()
OWN_429: set[str] = set()


def fake_upstream(name: str):
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        auth = headers.get("authorization", "")
        key = auth[7:] if auth.lower().startswith("bearer ") else ""
        path = scope.get("path", "/")
        if path.endswith("/v1/models"):
            payload = b'{"data":[]}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] == "http.request":
                body += message.get("body", b"") or b""
                if not message.get("more_body"):
                    break
        SEEN.append((name, key, path))
        rid = headers.get("x-request-id", "")
        if rid:
            RID_SEEN.append(rid)
        if name in DEAD:
            payload = b'{"error":{"message":"upstream dead"}}'
            await send({"type": "http.response.start", "status": 502,
                        "headers": [(b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})
            return
        if name in QUOTA_429:
            payload = (b'{"error":{"message":"free usage limit reached",'
                       b'"type":"FreeUsageLimitError"}}')
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})
            return
        if name in OWN_429:
            payload = (b'{"error":{"message":"gateway rate limited",'
                       b'"code":"rate_limit_exceeded"}}')
            await send({"type": "http.response.start", "status": 429,
                        "headers": [(b"content-length", str(len(payload)).encode())]})
            await send({"type": "http.response.body", "body": payload})
            return
        if SLOW.get(name):
            await asyncio.sleep(SLOW[name])
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")]})
        for chunk in (b'data: {"up":"%s"}\n\n' % name.encode(), b"data: [DONE]\n\n"):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


class RoutedTransport(httpx.AsyncBaseTransport):
    """Sends each request to the fake gateway matching its hostname."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        target = ROUTES.get(host)
        if target is None:
            raise httpx.ConnectError(f"no route for {host}")
        inner = httpx.ASGITransport(app=target)
        return await inner.handle_async_request(request)

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset():
    SEEN.clear()
    RID_SEEN.clear()
    ROUTES.clear()
    DEAD.clear()
    SLOW.clear()
    QUOTA_429.clear()
    OWN_429.clear()
    ROUTES["a"] = fake_upstream("a")
    ROUTES["b"] = fake_upstream("b")
    real = lb.make_client

    def patched() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=RoutedTransport(), follow_redirects=False)

    lb.make_client = patched
    yield
    lb.make_client = real


def two_upstream_pool() -> lb.Pool:
    return lb.Pool([lb.Upstream("a", "http://a", "key-A"),
                    lb.Upstream("b", "http://b", "key-B")])


async def call(app, key: str = "lb-secret", path: str = "/v1/chat/completions") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://lb") as c:
        return await c.post(path, json={"model": "m", "messages": []},
                            headers={"Authorization": f"Bearer {key}"})


@pytest.mark.asyncio
async def test_wrong_lb_key_rejected_401():
    resp = await call(lb.create_app(two_upstream_pool()), key="nope")
    assert resp.status_code == 401
    assert SEEN == []


@pytest.mark.asyncio
async def test_authorization_rewritten_per_upstream():
    pool = two_upstream_pool()
    app = lb.create_app(pool)
    for _ in range(4):
        assert (await call(app)).status_code == 200
    by_name = {name: key for name, key, _ in SEEN}
    assert by_name["a"] == "key-A"
    assert by_name["b"] == "key-B"


@pytest.mark.asyncio
async def test_both_upstreams_receive_traffic():
    pool = two_upstream_pool()
    app = lb.create_app(pool)
    for _ in range(6):
        await call(app)
    names = [n for n, _, _ in SEEN]
    assert names.count("a") == 3
    assert names.count("b") == 3


@pytest.mark.asyncio
async def test_least_conn_prefers_idle_upstream():
    pool = two_upstream_pool()
    pool.upstreams[0].inflight = 5
    await call(lb.create_app(pool))
    assert SEEN[-1][0] == "b"


@pytest.mark.asyncio
async def test_dead_upstream_fails_over_to_live_one():
    DEAD.add("a")
    pool = two_upstream_pool()
    resp = await call(lb.create_app(pool))
    assert resp.status_code == 200
    assert SEEN[0][0] == "a"  # tried first, got 502
    assert SEEN[1][0] == "b"  # retried onto the live bucket
    assert pool.upstreams[0].total_failures >= 1
    assert pool.upstreams[1].total_failures == 0


@pytest.mark.asyncio
async def test_all_upstreams_down_returns_502_not_5xx_leak():
    DEAD.update({"a", "b"})
    resp = await call(lb.create_app(two_upstream_pool()))
    assert resp.status_code == 502
    assert "upstream_unreachable" in resp.text


@pytest.mark.asyncio
async def test_client_body_never_leaks_lb_key_upstream():
    await call(lb.create_app(two_upstream_pool()))
    for _name, key, _path in SEEN:
        assert key != "lb-secret"


@pytest.mark.asyncio
async def test_sse_stream_relayed_intact():
    resp = await call(lb.create_app(two_upstream_pool()))
    assert resp.status_code == 200
    assert resp.text.endswith("data: [DONE]\n\n")
    assert '"up":"' in resp.text


@pytest.mark.asyncio
async def test_healthz_reports_per_upstream_state():
    DEAD.add("a")
    pool = two_upstream_pool()
    await call(lb.create_app(pool))
    transport = httpx.ASGITransport(app=lb.create_app(pool))
    async with httpx.AsyncClient(transport=transport, base_url="http://lb") as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert {u["name"] for u in body["upstreams"]} == {"a", "b"}


@pytest.mark.asyncio
async def test_concurrent_requests_spread_across_buckets():
    pool = two_upstream_pool()
    app = lb.create_app(pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://lb") as c:
        headers = {"Authorization": "Bearer lb-secret"}
        resps = await asyncio.gather(*[
            c.post("/v1/chat/completions", json={"model": "m", "messages": []},
                   headers=headers) for _ in range(8)
        ])
    assert all(r.status_code == 200 for r in resps)
    names = [n for n, _, _ in SEEN]
    assert names.count("a") + names.count("b") == 8
    assert names.count("a") > 0 and names.count("b") > 0


@pytest.mark.asyncio
async def test_quota_429_cordons_bucket_and_retries_next():
    QUOTA_429.add("a")
    pool = two_upstream_pool()
    resp = await call(lb.create_app(pool))
    assert resp.status_code == 200
    assert SEEN[0][0] == "a" and SEEN[1][0] == "b"
    assert pool.upstreams[0].quota_exhausted_until > 0
    assert pool.upstreams[0].quota_hits == 1
    assert pool.upstreams[1].quota_exhausted_until == 0
    # 被熔断的桶不再被调度；只剩一个可用桶时全部流量去 b
    before = len(SEEN)
    await call(_app(pool))
    assert all(name == "b" for name, _, _ in SEEN[before:])


def _app(pool):
    return lb.create_app(pool)


@pytest.mark.asyncio
async def test_quota_cordon_expires_at_utc_reset():
    up = lb.Upstream("a", "http://a", "key-A")
    up.quota_exhausted_until = 1.0  # 早已过期
    pool = lb.Pool([up])
    assert pool.candidates()[0].name == "a"
    assert up.quota_exhausted_until == 0.0


@pytest.mark.asyncio
async def test_all_buckets_cordoned_still_tries_and_relays_429():
    QUOTA_429.update({"a", "b"})
    pool = two_upstream_pool()
    resp = await call(lb.create_app(pool))
    assert resp.status_code == 429
    assert "FreeUsageLimitError" in resp.text


@pytest.mark.asyncio
async def test_own_rate_limit_429_does_not_cordon():
    OWN_429.add("a")
    pool = two_upstream_pool()
    resp = await call(lb.create_app(pool))
    assert resp.status_code == 200
    assert SEEN[0][0] == "a" and SEEN[1][0] == "b"
    assert pool.upstreams[0].quota_exhausted_until == 0


@pytest.mark.asyncio
async def test_request_id_forwarded_and_echoed():
    pool = two_upstream_pool()
    resp = await call(lb.create_app(pool))
    assert resp.status_code == 200
    assert resp.headers.get("x-request-id")
    assert RID_SEEN and all(RID_SEEN)


@pytest.mark.asyncio
async def test_admin_page_requires_key():
    pool = two_upstream_pool()
    transport = httpx.ASGITransport(app=lb.create_app(pool))
    async with httpx.AsyncClient(transport=transport, base_url="http://lb") as c:
        denied = await c.get("/admin")
        ok_json = await c.get("/admin/json?key=lb-secret")
        ok_html = await c.get("/admin?key=lb-secret")
    assert denied.status_code == 401
    assert ok_json.status_code == 200 and ok_json.json()["lb"] is True
    assert ok_html.status_code == 200
    assert "zen_lb buckets" in ok_html.text


@pytest.mark.asyncio
async def test_daily_request_counter_per_utc_day():
    pool = two_upstream_pool()
    app = lb.create_app(pool)
    for _ in range(3):
        await call(app)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for up in pool.upstreams:
        assert up.daily_date == today
        assert up.daily_requests >= 1
