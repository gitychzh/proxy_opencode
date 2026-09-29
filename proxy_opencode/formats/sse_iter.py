"""Protocol converters sitting in front of the OpenAI chat pipeline.

Each protocol module converts *into* an OpenAI chat-completions payload on
the way in, and converts the OpenAI response (JSON or SSE chunk stream)
*back out* into its own wire format. The upstream adapters stay untouched.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator


async def parse_sse_chunks(
    chunks: AsyncIterator[bytes],
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON payloads from an OpenAI-style SSE byte stream.

    Tolerates lines split across chunk boundaries and the non-JSON tail
    chunks some upstreams append.
    """
    buffer = b""
    async for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            data = _data_payload(line)
            if data is not None:
                yield data
    if buffer:
        data = _data_payload(buffer)
        if data is not None:
            yield data


def _data_payload(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
