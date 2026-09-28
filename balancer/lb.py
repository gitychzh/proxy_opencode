"""Least-connection load balancer in front of two OpenAI-compatible gateways.

Each upstream is a separate proxy_opencode instance with its own egress IP
(and therefore its own anonymous free-tier quota bucket) and its own inbound
bearer key. The balancer presents one key to clients and rewrites
Authorization per upstream, which is why this is a Python reverse proxy and
not nginx: nginx cannot vary proxy_set_header per upstream server.

Selection is least-connections with round-robin tie-breaking, so a slow or
stalled bucket drains away instead of queueing behind it. Upstreams are
health-probed in the background and skipped while failing; a request is
retried on another upstream only while nothing has been received from the
failed one, so a partial response is never duplicated into the client stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn

LOG_PATH = os.environ.get("ZEN_LB_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "lb.log")
LB_KEY = os.environ.get("ZEN_LB_API_KEY", "")
BIND_HOST = os.environ.get("ZEN_LB_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("ZEN_LB_PORT", "7892"))
HEALTH_INTERVAL_S = float(os.environ.get("ZEN_LB_HEALTH_INTERVAL", "20"))
HEALTH_FAIL_THRESHOLD = int(os.environ.get("ZEN_LB_HEALTH_FAILS", "3"))
CONNECT_TIMEOUT_S = float(os.environ.get("ZEN_LB_CONNECT_TIMEOUT", "15"))
READ_TIMEOUT_S = float(os.environ.get("ZEN_LB_READ_TIMEOUT", "600"))
WRITE_TIMEOUT_S = float(os.environ.get("ZEN_LB_WRITE_TIMEOUT", "60"))
MAX_INFLIGHT_PER_UPSTREAM = int(os.environ.get("ZEN_LB_MAX_INFLIGHT", "64"))

# No usable default: the buckets' credentials differ per deployment and must
# come from the environment (see balancer/run.cmd.example). An empty value
# means "no upstream configured" and the balancer fails closed with 503,
# rather than silently proxying with a baked-in key.
DEFAULT_UPSTREAMS = ""

HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
})

RETRYABLE_STATUS = frozenset({502, 503, 504})

logger = logging.getLogger("zen_lb")


def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_PATH, maxBytes=8 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    root.addHandler(logging.StreamHandler())


@dataclass
class Upstream:
    name: str
    base_url: str
    api_key: str
    inflight: int = 0
    consecutive_failures: int = 0
    healthy: bool = True
    total_requests: int = 0
    total_failures: int = 0
    last_error: str = ""
    last_latency_ms: float = 0.0
    # Quota-aware circuit breaking: the free tier allows ~600 requests per
    # egress IP per UTC day and answers 429 once it is burned. When that
    # happens the bucket is cordoned until the next UTC midnight (= 08:00
    # Beijing), because retrying into it only burns latency.
    quota_exhausted_until: float = 0.0
    quota_hits: int = 0
    daily_date: str = ""
    daily_requests: int = 0
    _quota_reason: str = field(default="", repr=False)

    @property
    def host_port(self) -> str:
        p = urlsplit(self.base_url)
        return f"{p.hostname}:{p.port or 80}"

    def target(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


def load_upstreams(spec: str | None = None) -> list[Upstream]:
    raw = spec if spec is not None else os.environ.get("ZEN_LB_UPSTREAMS", DEFAULT_UPSTREAMS)
    out: list[Upstream] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("|")
        if len(parts) != 3:
            raise ValueError(f"bad upstream spec (need name|url|key): {chunk!r}")
        name, url, key = (p.strip() for p in parts)
        if not name or not url or not key:
            raise ValueError(f"empty field in upstream spec: {chunk!r}")
        out.append(Upstream(name=name, base_url=url.rstrip("/"), api_key=key))
    if not out:
        raise ValueError("no upstreams configured")
    return out


def next_utc_reset(now: float | None = None) -> float:
    """Epoch timestamp of the next UTC midnight (free-tier quota reset)."""
    ts = now if now is not None else time.time()
    day_start = (int(ts) // 86400) * 86400
    return float(day_start + 86400)


def _is_own_rate_limit(body: bytes) -> bool:
    """Distinguish the bucket gateway's own 429 from the upstream quota 429.

    The gateway's security layer emits code=rate_limit_exceeded; the Zen free
    tier emits FreeUsageLimitError (relayed verbatim). Only the latter means
    the egress-IP quota for this bucket is gone.
    """
    low = body.lower()
    return b"rate_limit_exceeded" in low or b"gateway rate limited" in low


class Pool:
    def __init__(self, upstreams: list[Upstream]) -> None:
        self.upstreams = upstreams
        self._rr = itertools.count()
        self._lock = asyncio.Lock()

    def _clear_expired_quota(self) -> None:
        now = time.time()
        for u in self.upstreams:
            if u.quota_exhausted_until and now >= u.quota_exhausted_until:
                logger.info("upstream %s quota window elapsed, re-arming", u.name)
                u.quota_exhausted_until = 0.0
                u._quota_reason = ""

    def candidates(self) -> list[Upstream]:
        self._clear_expired_quota()
        usable = [u for u in self.upstreams if u.healthy and not u.quota_exhausted_until]
        if not usable:
            # Degraded fallbacks keep old failover semantics: prefer buckets
            # that merely flunked health probes; if every bucket is cordoned
            # for quota, let the request through so the client sees the real
            # upstream 429 instead of an invented error.
            usable = [u for u in self.upstreams if not u.quota_exhausted_until] \
                or list(self.upstreams)
        offset = next(self._rr) % len(usable)
        rotated = usable[offset:] + usable[:offset]
        return sorted(rotated, key=lambda u: u.inflight)

    async def acquire(self, up: Upstream) -> None:
        async with self._lock:
            up.inflight += 1
            up.total_requests += 1
            today = time.strftime("%Y-%m-%d", time.gmtime())
            if up.daily_date != today:
                up.daily_date = today
                up.daily_requests = 0
            up.daily_requests += 1

    async def mark_quota_exhausted(self, up: Upstream, reason: str = "") -> None:
        async with self._lock:
            until = next_utc_reset()
            up.quota_exhausted_until = until
            up.quota_hits += 1
            up._quota_reason = reason[:200]
            logger.warning(
                "upstream %s QUOTA EXHAUSTED until %s (%s)",
                up.name, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
                reason[:120])

    async def release(self, up: Upstream, ok: bool, latency_ms: float, error: str = "") -> None:
        async with self._lock:
            up.inflight = max(0, up.inflight - 1)
            up.last_latency_ms = round(latency_ms, 1)
            if ok:
                up.consecutive_failures = 0
                up.healthy = True
            else:
                up.total_failures += 1
                up.consecutive_failures += 1
                up.last_error = error[:300]
                if up.consecutive_failures >= HEALTH_FAIL_THRESHOLD:
                    up.healthy = False

    async def mark_health(self, up: Upstream, ok: bool, detail: str) -> bool:
        async with self._lock:
            was = up.healthy
            if ok:
                up.consecutive_failures = 0
                up.healthy = True
            else:
                up.consecutive_failures += 1
                up.last_error = detail[:300]
                if up.consecutive_failures >= HEALTH_FAIL_THRESHOLD:
                    up.healthy = False
            return was != up.healthy

    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for u in self.upstreams:
            quota_left_min = max(0, int((u.quota_exhausted_until - now) / 60)) \
                if u.quota_exhausted_until else 0
            out.append({
                "name": u.name,
                "target": u.host_port,
                "healthy": u.healthy,
                "quota_exhausted": bool(u.quota_exhausted_until),
                "quota_reset_in_min": quota_left_min,
                "quota_hits": u.quota_hits,
                "daily_requests": u.daily_requests,
                "daily_date": u.daily_date,
                "inflight": u.inflight,
                "requests": u.total_requests,
                "failures": u.total_failures,
                "last_latency_ms": u.last_latency_ms,
                "last_error": u.last_error,
            })
        return out


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S,
                              write=WRITE_TIMEOUT_S, pool=CONNECT_TIMEOUT_S),
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        follow_redirects=False,
    )


def client_bearer(headers: dict[str, str]) -> str:
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def forwardable(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    skip = HOP_BY_HOP | {"host", "content-length", "authorization", "accept-encoding",
                         "x-request-id"}
    return [(n, v) for n, v in raw_headers if n.decode("latin-1").lower() not in skip]


def response_headers(raw_headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    skip = HOP_BY_HOP | {"content-length"}
    return [(n, v) for n, v in raw_headers if n.decode("latin-1").lower() not in skip]


async def read_body(receive: Any) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        kind = message["type"]
        if kind == "http.disconnect":
            break
        if kind == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                break
    return b"".join(chunks)


async def health_loop(pool: Pool, client: httpx.AsyncClient) -> None:
    while True:
        try:
            for up in pool.upstreams:
                t0 = time.perf_counter()
                try:
                    r = await client.get(
                        up.target("/v1/models"),
                        headers={"Authorization": f"Bearer {up.api_key}"},
                        timeout=httpx.Timeout(15.0, connect=CONNECT_TIMEOUT_S),
                    )
                    ok = r.status_code in (200, 401)
                    detail = f"status={r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    ok, detail = False, f"{type(exc).__name__}: {exc}"
                changed = await pool.mark_health(up, ok, detail)
                if changed:
                    logger.warning("upstream %s healthy=%s (%s)", up.name, up.healthy, detail)
                logger.info("health %s ok=%s %s %.0fms", up.name, ok, detail,
                            (time.perf_counter() - t0) * 1000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("health loop error: %s", exc)
        await asyncio.sleep(HEALTH_INTERVAL_S)


def admin_page(snap: list[dict[str, Any]]) -> str:
    rows = []
    for u in snap:
        quota = ("exhausted · reset in %dmin" % u["quota_reset_in_min"]) \
            if u["quota_exhausted"] else "ok"
        badge = "#a32d2d" if u["quota_exhausted"] or not u["healthy"] else "#3b6d11"
        rows.append(
            f"<tr><td>{u['name']}</td><td>{u['target']}</td>"
            f"<td style='color:{badge}'>{'up' if u['healthy'] else 'down'}</td>"
            f"<td style='color:{badge}'>{quota}</td>"
            f"<td>{u['daily_date'] or '-'} / {u['daily_requests']}</td>"
            f"<td>{u['inflight']}</td><td>{u['requests']}</td>"
            f"<td>{u['failures']}</td><td>{u['last_latency_ms']:.0f}</td>"
            f"<td class='err'>{u['last_error'][:80]}</td></tr>")
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<title>zen_lb admin</title><meta http-equiv='refresh' content='5'>"
        "<style>body{font:13px/1.5 system-ui,sans-serif;margin:24px;color:#222}"
        "table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px 10px;"
        "text-align:left}th{background:#f4f4f4}.err{color:#888;max-width:280px;"
        "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}</style>"
        "<h2>zen_lb buckets</h2><table><tr><th>bucket</th><th>target</th>"
        "<th>health</th><th>quota</th><th>UTC day / reqs</th><th>inflight</th>"
        "<th>total</th><th>failures</th><th>ms</th><th>last error</th></tr>"
        + "".join(rows) + "</table>"
        f"<p>refreshed {time.strftime('%Y-%m-%d %H:%M:%S')} (auto every 5s)</p>")


def create_app(pool: Pool) -> Any:
    client = make_client()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    scope.setdefault("state", {})["_health"] = asyncio.create_task(
                        health_loop(pool, client))
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    task = scope.get("state", {}).get("_health")
                    if task:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    await client.aclose()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        started = time.perf_counter()

        if path in ("/healthz", "/__lb_health"):
            snap = pool.snapshot()
            any_ok = any(u["healthy"] and not u["quota_exhausted"] for u in snap) \
                or any(u["healthy"] for u in snap)
            payload = json.dumps({"healthy": any_ok, "upstreams": snap},
                                 ensure_ascii=False).encode()
            await send({"type": "http.response.start",
                        "status": 200 if any_ok else 503,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return

        if not LB_KEY:
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({
                "error": {"message": "ZEN_LB_API_KEY not set; balancer is locked",
                          "type": "configuration_error"}}).encode()})
            return

        authorized = client_bearer(headers) == LB_KEY
        if not authorized and path.startswith("/admin"):
            # Browser-friendly admin auth: /admin?key=...
            from urllib.parse import parse_qs
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            authorized = any(v == LB_KEY for v in qs.get("key", []))
        if not authorized:
            logger.warning("lb auth rejected %s %s client=%s", method, path,
                           (scope.get("client") or ("-",))[0])
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({
                "error": {"message": "Invalid API key", "type": "authentication_error",
                          "code": "invalid_key"}}).encode()})
            return

        if path == "/admin":
            snap = pool.snapshot()
            body = admin_page(snap).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": body})
            return
        if path == "/admin/json":
            snap = pool.snapshot()
            payload = json.dumps({"lb": True, "now": time.time(),
                                  "upstreams": snap}, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return

        rid = headers.get("x-request-id") or uuid.uuid4().hex[:16]
        body = await read_body(receive)
        base_headers = forwardable(scope["headers"]) + [(b"x-request-id", rid.encode())]
        tried = 0
        last_error = "no upstream attempted"
        quota_429_body = b""  # 全部桶都额度耗尽时，把真实 429 透传给客户端

        for up in pool.candidates():
            if tried >= len(pool.upstreams):
                break
            if up.inflight >= MAX_INFLIGHT_PER_UPSTREAM and len(pool.upstreams) > 1:
                logger.warning("skipping saturated upstream %s inflight=%d", up.name, up.inflight)
                continue
            tried += 1
            await pool.acquire(up)
            t0 = time.perf_counter()
            streamed = False
            try:
                headers_fwd = base_headers + [
                    (b"authorization", f"Bearer {up.api_key}".encode()),
                    (b"content-length", str(len(body)).encode()),
                ]
                resp = await client.send(
                    client.build_request(method, up.target(path), content=body,
                                         headers=headers_fwd),
                    stream=True)
                if resp.status_code == 429:
                    err = await resp.aread()
                    await resp.aclose()
                    last_error = f"{up.name} status=429 body={err[:200]!r}"
                    await pool.release(up, False, (time.perf_counter() - t0) * 1000,
                                       last_error)
                    if _is_own_rate_limit(err):
                        logger.warning("rid=%s upstream %s own rate limit, retry next",
                                       rid, up.name)
                    else:
                        quota_429_body = err
                        await pool.mark_quota_exhausted(
                            up, err[:200].decode("utf-8", errors="replace"))
                        logger.warning("rid=%s upstream %s cordoned for quota, retry next",
                                       rid, up.name)
                    continue
                if resp.status_code in RETRYABLE_STATUS:
                    err = await resp.aread()
                    await resp.aclose()
                    last_error = f"{up.name} status={resp.status_code} body={err[:200]!r}"
                    await pool.release(up, False, (time.perf_counter() - t0) * 1000, last_error)
                    logger.warning("lb retryable %s from %s", resp.status_code, up.name)
                    continue
                logger.info("rid=%s lb %s %s -> %s status=%d attempt=%d", rid, method,
                            path, up.name, resp.status_code, tried)
                out_headers = response_headers(resp.headers.raw) + [
                    (b"x-request-id", rid.encode())]
                await send({"type": "http.response.start", "status": resp.status_code,
                            "headers": out_headers})
                streamed = True
                async for chunk in resp.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk,
                                    "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                await pool.release(up, True, (time.perf_counter() - t0) * 1000)
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.PoolTimeout) as exc:
                last_error = f"{up.name} {type(exc).__name__}: {exc}"
                await pool.release(up, False, (time.perf_counter() - t0) * 1000, last_error)
                if streamed:
                    raise
                logger.warning("lb transport failure %s: %s", up.name, last_error)
                continue
            except Exception as exc:  # noqa: BLE001
                last_error = f"{up.name} {type(exc).__name__}: {exc}"
                await pool.release(up, False, (time.perf_counter() - t0) * 1000, last_error)
                logger.exception("lb error on %s", up.name)
                if streamed:
                    raise
                continue

        logger.error("rid=%s lb all upstreams failed %s %s attempts=%d last=%s",
                     rid, method, path, tried, last_error)
        if quota_429_body:
            # Every bucket is out of free quota: relay the upstream's real 429
            # (with reset semantics) instead of masking it as a 502.
            with contextlib.suppress(Exception):
                await send({"type": "http.response.start", "status": 429,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"x-request-id", rid.encode())]})
                await send({"type": "http.response.body", "body": quota_429_body})
            return
        with contextlib.suppress(Exception):
            await send({"type": "http.response.start", "status": 502,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"x-request-id", rid.encode())]})
            await send({"type": "http.response.body", "body": json.dumps({
                "error": {"message": f"All gateway upstreams are unavailable: {last_error}",
                          "type": "api_connection_error",
                          "code": "upstream_unreachable"}}).encode()})
        logger.info("rid=%s lb %s %s FAILED %.0fms (%s)", rid, method, path,
                    (time.perf_counter() - started) * 1000, last_error)

    return app


def main() -> None:
    setup_logging()
    if BIND_HOST not in ("127.0.0.1", "localhost", "::1") and not LB_KEY:
        raise SystemExit("refusing to bind a non-loopback host without ZEN_LB_API_KEY")
    pool = Pool(load_upstreams())
    logger.info("zen_lb starting bind=%s:%d lb_key=%s upstreams=%s",
                BIND_HOST, BIND_PORT, "set" if LB_KEY else "MISSING",
                [(u.name, u.host_port) for u in pool.upstreams])
    server = uvicorn.Server(uvicorn.Config(
        create_app(pool), host=BIND_HOST, port=BIND_PORT,
        log_level="warning", access_log=False, timeout_keep_alive=75))
    server.run()


if __name__ == "__main__":
    main()
