"""zen-direct adapter: one upstream attempt, bucket failover, transport choice.

Scope of this module — orchestration only. The protocol facts it composes live
next to it: :mod:`.protocol` (headers/ids), :mod:`.prompt` (body gate),
:mod:`.tools` (shape gate), :mod:`.body` (composition), :mod:`.aggregate`
(non-streaming replies), :mod:`.errors` (free-tier rewrites).

Responsibilities:

* **Always stream upstream.** The free tier 403s ``stream: false``, so a
  non-streaming client is served by aggregating the SSE.
* **Bucket failover.** 429 / 5xx / transport failure / free-tier 403 release
  the lease and retry on the next bucket — none of these depend on the request
  and no client byte has been sent yet. The loop is bounded so a drained pool
  answers instead of hanging. Never retried after bytes reached the client.
* **Per-attempt session mint.** Headers are rebuilt inside the loop: upstream
  sessions are single-use, and a replay returns the quota-shaped 429.
* **Transport choice.** bun (boringssl/HTTP2) is required by the transport
  fingerprint gate; httpx remains the dev/test fallback.

Authority for every rule above: AGENTS.md 硬约束 1.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping
from urllib.parse import quote_plus, urlparse

import httpx

from ...config import Settings
from ...egress.models import HARD_QUOTA_RETRY_AFTER_S
from ...egress.pool import EgressPool, Lease
from ...model_profile import profile as model_profile
from ..openai_http import ResponseLike, StreamRelay, _raw_relay_body
from ..zen_bun_transport import BunTransport, BunTransportError
from .aggregate import StreamStalled, aggregate_sse
from .body import build_body
from .errors import (
    egress_unavailable_result,
    is_free_tier_rejection,
    quota_exhausted_result,
)
from .policy import Applied, apply_request_policy
from .protocol import build_headers, retry_after_seconds
from .tools import builtin_tools

logger = logging.getLogger("proxy_opencode.zen")

# Bucket-failover cap for 429 / 5xx / free-tier 403 / transport retries. A
# fully-drained pool must still surface its last upstream error instead of
# looping until the client times out.
FAILOVER_MAX_ATTEMPTS = 3

# StreamRelay close-hook outcome -> (pool release ok, kind).
#
# Only transport-level failures count against the egress bucket: a stalled or
# interrupted stream means the tunnel/upstream path stopped delivering, and
# three consecutive failures cordon the bucket. A clean upstream cut ("cut") is
# an upstream-content failure a healthy tunnel merely forwarded. Unknown
# outcomes default to failure so a new outcome can never count as success.
STREAM_OUTCOMES: dict[str, tuple[bool, str]] = {
    "completed": (True, ""),
    "disconnected": (True, "client_disconnected"),
    "cut": (True, "stream_cut"),
    "stalled": (False, "stream_stalled"),
    "interrupted": (False, "stream_interrupted"),
}

_ALLOWED_UPSTREAM_HOSTS = ("opencode.ai", "localhost", "127.0.0.1")


class ZenDirectAdapter:
    """OpenAI-compatible adapter speaking the opencode client protocol."""

    name = "zen-direct"

    def __init__(self, settings: Settings, pool: EgressPool | None = None) -> None:
        self._settings = settings
        # When the pool is configured, chat leaves through the per-bucket
        # device agents instead of this local client (the container has no
        # free-tier quota — every bucket is a device's public IP).
        self._pool = pool
        base = settings.zen_base_url
        if (urlparse(base).hostname or "") not in _ALLOWED_UPSTREAM_HOSTS:
            raise ValueError(
                "OPENCODE_ZEN_BASE_URL host not allowed for zen-direct "
                f"(got {base!r}; use UPSTREAM_MODE=openai for arbitrary hosts)"
            )
        # Absolute upstream base for request URLs: the pool's per-egress
        # clients carry no base_url (they exist only to route through a device
        # proxy), so paths must be absolute or httpx raises.
        self._base = base.rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(float(settings.zen_timeout_s), connect=15.0),
            proxy=settings.zen_proxy or None,
            # trust_env stays False by default: following ambient HTTP_PROXY
            # couples the egress path to machine state (measured 2026-10-01).
            trust_env=settings.upstream_trust_env,
        )
        self._auth_free = not settings.zen_api_key
        # 40-hex workspace hash for x-opencode-project; a captured genuine
        # value can be pinned via OPENCODE_ZEN_PROJECT_ID, else a fixed sha1
        # keeps the header byte-stable.
        self._project_id = settings.zen_project_id
        self._builtin_tools = builtin_tools()
        self._bun = BunTransport.from_settings(settings)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ API

    async def list_models(self) -> dict[str, Any]:
        data: list[dict[str, Any]] = []
        try:
            resp = await self.client.get(
                "/models", headers=self._headers(request_id=False)
            )
            if resp.status_code == 200:
                body = resp.json()
                for m in body.get("data", []):
                    if isinstance(m, dict) and m.get("id"):
                        data.append(
                            {
                                "id": f"opencode/{m['id']}",
                                "object": "model",
                                "created": 0,
                                "owned_by": "opencode-zen",
                            }
                        )
        except httpx.HTTPError:
            logger.warning("zen /models unreachable; falling back to static list")
        if not data:
            data = [
                {
                    "id": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": "opencode-zen",
                }
                for m in self._settings.zen_models
            ]
        return {
            "object": "list",
            "data": data,
            "metadata": {"adapter": self.name, "source": "opencode-zen"},
        }

    async def chat(self, payload: dict[str, Any]) -> Any:
        """Handle one chat completion (see the module docstring for the rules).

        Returns a JSON body, a ``StreamRelay`` for streaming clients, or an
        error-relay dict shaped ``{"__status__": int, "content": {...}}``.
        """

        model = self._zen_model(payload.get("model"))
        body = self._build_body(payload, model)
        body["stream"] = True
        applied = self._apply_policy(body, model)
        stream = bool(payload.get("stream"))
        # Estimated prompt tokens for this request. Stamped onto every lease
        # below so the bucket's daily TOKEN budget can be metered — the
        # upstream quota is spent in tokens, not calls (2026-10-28).
        estimated = applied.estimated_input

        if self._pool is None:
            result = await self._exchange(
                self.client, body, self._headers(), model, stream, None
            )
            return self._egress_gate_answer(result)

        # Pool-wide quota blackout: the allowance is account-scoped, so a
        # fresh IP cannot rescue it. Answering here costs milliseconds and
        # hands the request to the fallback chain at once, instead of walking
        # every benched bucket (one upstream RTT each, ~1s apiece).
        blackout_probe = getattr(self._pool, "quota_blackout_s", None)
        blackout = float(blackout_probe()) if callable(blackout_probe) else 0.0
        if blackout > 0:
            logger.warning(
                "egress pool in quota blackout; skipping upstream",
                extra={"blackout_s": round(blackout, 1)},
            )
            return quota_exhausted_result(blackout)

        attempts = min(self._pool.size, FAILOVER_MAX_ATTEMPTS)

        async def _acquire() -> Lease:
            """Acquire a bucket and stamp this request's token estimate."""

            held = await self._pool.acquire()  # type: ignore[union-attr]
            held.tokens = estimated
            return held

        lease = await _acquire()
        while True:
            try:
                result = await self._exchange(
                    lease.client, body, self._headers(), model, stream, lease
                )
            except (httpx.HTTPError, ConnectionError) as exc:
                # _exchange already released the lease as a transport failure
                # (it wraps httpx transport errors in a plain ConnectionError).
                attempts -= 1
                # A stalled aggregate is a bucket verdict, not a generic
                # transport fault: keep the two kinds apart so the daily
                # report can tell a silent tunnel from a broken socket.
                kind = "stream_stalled" if isinstance(exc, StreamStalled) else "transport"
                if attempts <= 0:
                    raise _transport_error(exc) from exc
                _log_failover(kind=kind, remaining=attempts, detail=str(exc)[:120])
                lease = await _acquire()
                continue
            except BaseException:
                # Cancellation (client hung up before any byte) or an
                # unexpected adapter fault: hand the in-flight slot back
                # without judging the bucket. ``abandon`` is a no-op when
                # _exchange already released the lease.
                self._pool.abandon(lease)
                raise
            if isinstance(result, StreamRelay):
                return result
            status = result.get("__status__")
            if status == 429:
                attempts -= 1
                # The bucket that just answered 429 is already cordoned. If
                # that left the whole pool benched on quota verdicts, the
                # remaining attempts are guaranteed to fail too: report the
                # blackout here so the *first* request of an exhausted day
                # also stops early and the fallback chain gets the reset
                # deadline instead of a flat hold.
                # Response evidence first: a 429 whose Retry-After sits past
                # the throttle boundary is an account-wide allowance verdict
                # (verified 2026-10-29 against a freshly rebooted IP), so the
                # sibling buckets would answer identically — trying them only
                # adds an upstream RTT each (3 attempts measured 11.8s on the
                # first request of an exhausted day).
                hinted = result.get("__quota_blackout_s__")
                if isinstance(hinted, (int, float)) and hinted > 0:
                    logger.warning(
                        "quota verdict on the first 429; stopping retries",
                        extra={"blackout_s": round(float(hinted), 1)},
                    )
                    return quota_exhausted_result(float(hinted))
                blackout = float(blackout_probe()) if callable(blackout_probe) else 0.0
                if blackout > 0:
                    logger.warning(
                        "egress pool in quota blackout after 429; stopping retries",
                        extra={"blackout_s": round(blackout, 1)},
                    )
                    return quota_exhausted_result(blackout)
                if attempts <= 0:
                    return result
                _log_failover(kind="http_429", remaining=attempts)
                lease = await _acquire()
                continue
            if isinstance(status, int) and status >= 500:
                attempts -= 1
                if attempts <= 0:
                    return result
                _log_failover(kind=f"http_{status}", remaining=attempts)
                lease = await _acquire()
                continue
            if (
                self._auth_free
                and is_free_tier_rejection(status, result.get("content"))
            ):
                # Defensive failover on a gate rejection: with the four-real-
                # name toolset this should stay cold; if the gate tightens
                # again, cordons + the rotator recover automatically.
                attempts -= 1
                if attempts <= 0:
                    return self._egress_gate_answer(result)
                _log_failover(kind="http_403_free_tier", remaining=attempts)
                lease = await _acquire()
                continue
            # 200 body, or a request-level 4xx: final for this request.
            return result

    def _egress_gate_answer(self, result: Any) -> Any:
        """Map a final anonymous free-tier 403 onto a retryable 503.

        A relayed 403 makes clients blame their own credentials and stop
        retrying; keyed mode and other 403 bodies pass through untouched
        because those really are request-level answers.
        """

        if (
            isinstance(result, dict)
            and self._auth_free
            and is_free_tier_rejection(result.get("__status__"), result.get("content"))
        ):
            logger.warning("free tier egress gate final; answering 503 to the client")
            return egress_unavailable_result()
        return result

    # ------------------------------------------------------------- transport

    async def _exchange(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Route one upstream attempt through the configured transport."""

        if self._bun is not None:
            return await self._exchange_via_bun(
                self._bun, body, headers, model, stream, lease
            )
        return await self._exchange_via_httpx(
            client, body, headers, model, stream, lease
        )

    async def _exchange_via_bun(
        self,
        bun: BunTransport,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Send one attempt through the bun forwarder (production stack)."""

        try:
            resp = await bun.post(
                base_url=self._base,
                path="/chat/completions",
                headers=headers,
                body=json.dumps(body).encode("utf-8"),
                proxy_url=self._egress_proxy_url(lease),
                timeout_s=float(self._settings.zen_timeout_s),
            )
        except BunTransportError as exc:
            await self._release(lease, ok=False, kind="transport")
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = b"".join([chunk async for chunk in resp.aiter_bytes()])
            result = _raw_relay_body(resp.status_code, raw)
            await self._release_for_status(resp.status_code, resp.headers, result, lease)
            return result
        return await self._consume_ok(resp, model, stream, lease)

    async def _exchange_via_httpx(
        self,
        client: httpx.AsyncClient,
        body: dict[str, Any],
        headers: dict[str, str],
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Send one attempt with httpx (dev/tests, no-bun images)."""

        req = client.build_request(
            "POST", f"{self._base}/chat/completions", json=body, headers=headers
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await self._release(lease, ok=False, kind="transport")
            raise ConnectionError(f"zen request failed: {exc}") from exc
        if resp.status_code != 200:
            raw = await resp.aread()
            await resp.aclose()
            result = _raw_relay_body(resp.status_code, raw)
            await self._release_for_status(resp.status_code, resp.headers, result, lease)
            return result
        return await self._consume_ok(resp, model, stream, lease)

    async def _consume_ok(
        self,
        resp: ResponseLike,
        model: str,
        stream: bool,
        lease: Lease | None,
    ) -> Any:
        """Relay a streaming answer, or aggregate a non-streaming one."""

        if stream:
            return self._stream_relay(resp, float(self._settings.stream_stall_s), lease)
        try:
            result = await aggregate_sse(
                resp, model, stall_s=float(self._settings.stream_stall_s)
            )
        except StreamStalled:
            # Released as its own kind: "aggregate_error" would fold a dead
            # tunnel into the same bucket-accounting line as a malformed
            # upstream body, and the two need different follow-up.
            await self._release(lease, ok=False, kind="stream_stalled")
            raise
        except Exception:
            await self._release(lease, ok=False, kind="aggregate_error")
            raise
        else:
            await self._release(lease, ok=True)
            return result
        finally:
            await resp.aclose()

    async def _release_for_status(
        self,
        status: int,
        headers: Mapping[str, str] | None,
        result: Any,
        lease: Lease | None,
    ) -> None:
        """Fold a non-200 upstream verdict into the lease.

        Classification is identical for both transports: 429 carries the
        quota verdict (with Retry-After), 5xx is an upstream fault, and a
        free-tier 403 is an egress-level rejection worth cordoning.
        """

        if status == 429:
            hard_after = retry_after_seconds(headers)
            if (
                hard_after is not None
                and hard_after > HARD_QUOTA_RETRY_AFTER_S
                and isinstance(result, dict)
            ):
                # The definitive per-response evidence of a daily-quota
                # verdict: a Retry-After beyond the throttle boundary. Stamp
                # it on the relay so the caller can hand the fallback chain a
                # real deadline even when the attempt budget ran out before
                # every bucket had been tried (3 attempts, 4 buckets).
                result["__quota_blackout_s__"] = float(hard_after)
            await self._release(
                lease,
                ok=False,
                kind="http_429",
                retry_after_s=hard_after,
            )
        elif status >= 500:
            await self._release(lease, ok=False, kind=f"http_{status}")
        elif (
            status == 403
            and self._auth_free
            and is_free_tier_rejection(status, result.get("content"))
        ):
            await self._release(lease, ok=False, kind="free_tier_rejected")
        else:
            await self._release(lease, ok=True)

    async def _release(
        self,
        lease: Lease | None,
        *,
        ok: bool,
        kind: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        if lease is not None and self._pool is not None:
            await self._pool.release(
                lease, ok=ok, kind=kind, retry_after_s=retry_after_s
            )

    def _stream_relay(
        self, resp: ResponseLike, stall_s: float, lease: Lease | None
    ) -> StreamRelay:
        """Build the streaming relay + pool release hook (shared transports)."""

        if lease is None:
            return StreamRelay(resp, {}, stall_s=stall_s)
        pool = self._pool
        assert pool is not None
        held: Lease = lease

        async def _released(outcome: str) -> None:
            ok, kind = STREAM_OUTCOMES.get(outcome, (False, outcome))
            if not ok:
                # Attribution: which egress stopped delivering? StreamRelay
                # already logged the mechanism; this ties it to the bucket.
                logger.warning(
                    "upstream stream failed mid-way",
                    extra={"egress": held.name, "reason": kind},
                )
            await pool.release(held, ok=ok, kind=kind)

        return StreamRelay(resp, {}, on_close=_released, stall_s=stall_s)

    def _egress_proxy_url(self, lease: Lease | None) -> str | None:
        """HTTP-proxy URL for the leased bucket's agent (bun subprocess env).

        Credentials come from ``zen_egress_secret`` and are composed per
        attempt, so no secret value is stored in specs, admin snapshots or
        logs.
        """

        if lease is None:
            return None
        agent = lease.state.spec.agent_url
        secret = self._settings.zen_egress_secret
        if not secret:
            return agent
        parsed = urlparse(agent)
        creds = quote_plus(secret)
        return f"{parsed.scheme}://{creds}:{creds}@{parsed.netloc}{parsed.path or ''}"

    # ------------------------------------------------------------- internals

    def _zen_model(self, model: Any) -> str:
        """Rewrite a requested model id to the upstream bare id.

        Resolution order:

        1. an exact ``ZEN_MODEL_MAP`` hit (explicit override);
        2. the ``PUBLIC_MODELS`` catalogue — its third field IS the upstream
           id, so a deployment that only publishes an alias still maps
           correctly. Without this step the prefix strip sent the alias's bare
           name upstream: ``deepseek-ai/deepseek-v4.1-flash`` became the PAID
           ``deepseek-v4.1-flash`` and came back ``401 AuthError`` (measured
           2026-10-29), which the failover chain then masked as a dead primary;
        3. the provider prefix stripped exactly like opencode does;
        4. empty → the first configured zen model.
        """

        raw = str(model or "")
        mapped = self._settings.zen_model_map.get(raw)
        if mapped:
            return mapped
        for entry in self._settings.public_models:
            if str(entry.get("id") or "") == raw:
                upstream = str(entry.get("upstream") or "")
                if upstream:
                    return upstream
        _provider, sep, model_id = raw.partition("/")
        return model_id if sep else (raw or self._default_model())

    def _default_model(self) -> str:
        models = self._settings.zen_models
        return models[0] if models else "big-pickle"

    def _headers(self, *, request_id: bool = True) -> dict[str, str]:
        """One attempt's protocol headers (new session id every call)."""

        return build_headers(
            client_version=self._settings.zen_client_version,
            bun_version=self._settings.zen_bun_version,
            project_id=self._project_id,
            api_key=self._settings.zen_api_key,
            request_id=request_id,
        )

    def _build_body(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        """Compose this request's upstream body (marker + tools + clean)."""

        return build_body(
            payload,
            model=model,
            auth_free=self._auth_free,
            marker_mode=self._settings.zen_marker_mode,
            tools_mode=self._settings.zen_tools_mode,
            builtins=self._builtin_tools,
        )

    def _apply_policy(self, body: dict[str, Any], model: str) -> Applied:
        """Apply the model's published parameters to the composed body.

        Runs after composition so the estimate includes the injected marker
        prompt and builtin tools — those are real prompt tokens and the
        context guard must see them. Raises :class:`ContextOverflow` (a
        client-fault 400 with ``context_length_exceeded``) without touching
        the network: refusing locally costs no free-tier quota.
        """

        applied = apply_request_policy(
            body,
            profile=model_profile(model),
            reasoning_policy=self._settings.zen_reasoning_policy,
            context_guard=self._settings.zen_context_guard,
            input_limit_override=self._settings.zen_context_input,
            output_budget_mode=self._settings.zen_output_budget,
        )
        if applied.dropped:
            # Field NAMES only — never their values (日志红线).
            logger.info(
                "thinking controls dropped: model publishes no effort levels",
                extra={"model": model, "dropped": ",".join(applied.dropped)},
            )
        if applied.clamped:
            logger.info(
                "max_tokens clamped to the room left in the context window",
                extra={
                    "model": model,
                    "max_tokens": applied.max_tokens,
                    "estimated_input": applied.estimated_input,
                },
            )
        return applied


def _log_failover(*, kind: str, remaining: int, detail: str = "") -> None:
    """Record one upstream failure handed to the bucket-failover loop.

    Counts live in the message body (they are not whitelisted extras); never
    any request content. ``detail`` carries the transport failure
    class/message — without it a 300s header stall on a dead tunnel left no
    trace at all (2026-10-25).
    """

    suffix = f" [{detail}]" if detail else ""
    logger.warning(
        f"egress failover: upstream {kind} on one bucket, "
        f"retrying on another ({remaining} attempt(s) left){suffix}"
    )


def _transport_error(exc: BaseException) -> ConnectionError:
    """Wrap a transport failure once (the inner layer already prefixes it)."""

    message = str(exc)
    if message.startswith("zen request failed"):
        return ConnectionError(message)
    return ConnectionError(f"zen request failed: {message}")


__all__ = [
    "FAILOVER_MAX_ATTEMPTS",
    "STREAM_OUTCOMES",
    "ZenDirectAdapter",
]
