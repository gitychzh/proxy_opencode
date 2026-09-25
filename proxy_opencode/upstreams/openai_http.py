"""Plain OpenAI-compatible HTTP passthrough adapter (UPSTREAM_MODE=openai).

Forwards whitelisted chat payloads to UPSTREAM_BASE_URL with
UPSTREAM_API_KEY. Streaming responses are relayed byte-for-byte.
"""

from __future__ import annotations

import json
import time
import uuid
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
        try:
            async for chunk in resp.aiter_bytes():
                # Best-effort capture of usage from streamed chunks for logging.
                if b"data:" in chunk and b"usage" in chunk:
                    for line in chunk.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:") and b"usage" in line:
                            try:
                                data = json.loads(line[5:])
                                if isinstance(data, dict) and data.get("usage"):
                                    self.usage_holder["usage"] = data["usage"]
                            except Exception:
                                pass
                yield chunk
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


def timestamps() -> tuple[int, str]:
    return int(time.time()), f"chatcmpl-{uuid.uuid4().hex[:16]}"
