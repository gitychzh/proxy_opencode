"""Plain OpenAI-compatible HTTP passthrough adapter (UPSTREAM_MODE=openai).

Forwards whitelisted chat payloads to UPSTREAM_BASE_URL with
UPSTREAM_API_KEY. Streaming responses are relayed byte-for-byte.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import Settings


class StreamRelay:
    """Wraps an upstream streaming response for the route layer to consume."""

    def __init__(self, resp: httpx.Response, usage_holder: dict[str, Any]) -> None:
        self.response = resp
        self.usage_holder = usage_holder

    async def chunks(self) -> AsyncIterator[bytes]:
        resp = self.response
        # Best-effort capture of usage from streamed chunks for logging. The
        # scan is line-buffered: a `data:` line carrying usage can be split
        # across any number of network chunks, and a naive per-chunk
        # `json.loads` silently drops it (observed with long tool-call
        # streams). Bytes are still relayed verbatim — only the parse is
        # buffered.
        pending = b""

        def scan_line(line: bytes) -> None:
            line = line.strip()
            if not line.startswith(b"data:") or b"usage" not in line:
                return
            try:
                data = json.loads(line[5:])
            except Exception:
                return
            if isinstance(data, dict) and data.get("usage"):
                self.usage_holder["usage"] = data["usage"]

        try:
            async for chunk in resp.aiter_bytes():
                if pending or b"usage" in chunk:
                    *lines, pending = (pending + chunk).split(b"\n")
                    for line in lines:
                        scan_line(line)
                yield chunk
            if pending:
                scan_line(pending)
        finally:
            await resp.aclose()


class OpenAIHttpAdapter:
    name = "openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers={
                "Authorization": f"Bearer {settings.upstream_api_key}",
                "Content-Type": "application/json",
            },
            # Never follow ambient proxy settings (see upstream_trust_env).
            trust_env=settings.upstream_trust_env,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def list_models(self) -> dict[str, Any] | httpx.Response:
        resp = await self.client.get("/v1/models")
        if resp.status_code != 200:
            return resp
        return resp.json()

    async def chat(self, payload: dict[str, Any]) -> Any:
        """Returns a JSON body, an httpx.Response (error relay), or a StreamRelay."""
        stream = bool(payload.get("stream"))
        if stream:
            req = self.client.build_request(
                "POST", "/v1/chat/completions", json=payload
            )
            try:
                resp = await self.client.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise ConnectionError(f"Upstream request failed: {exc}") from exc
            if resp.status_code != 200:
                body = await resp.aread()
                await resp.aclose()
                return _raw_relay_body(resp.status_code, body)
            return StreamRelay(resp, {})

        try:
            resp = await self.client.post("/v1/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ConnectionError(f"Upstream request failed: {exc}") from exc
        if resp.status_code != 200:
            return _raw_relay_body(resp.status_code, resp.content)
        try:
            return resp.json()
        except Exception:
            return _raw_relay_body(502, b"")


def _raw_relay_body(status: int, body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except Exception:
        data = None
    return {
        "__status__": status,
        "content": data
        if isinstance(data, dict)
        else {
            "error": {
                "message": f"Upstream error ({status}).",
                "type": "api_error",
                "param": None,
                "code": None,
            }
        },
    }
