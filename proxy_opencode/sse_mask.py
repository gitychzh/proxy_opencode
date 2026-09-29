"""SSE stream rewriting: scrub upstream model names out of relayed chunks.

Chat-completion SSE chunks carry a top-level `"model": "<id>"` field. When
model masking is on, every such field is rewritten to the public model id
before the chunk reaches the client. Line-buffered so JSON payloads split
across TCP chunks are handled correctly.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

_MODEL_FIELD = re.compile(rb'("model"\s*:\s*")[^"]*(")')


def rewrite_sse_line(line: bytes, public_model: str) -> bytes:
    """Rewrite the model field of one SSE data line, if present."""
    if line.startswith(b"data:") and b'"model"' in line:
        repl = _MODEL_FIELD.sub(
            rb"\g<1>" + public_model.encode("utf-8") + rb"\g<2>", line
        )
        return repl
    return line


async def mask_model_field(
    chunks: AsyncIterator[bytes], public_model: str
) -> AsyncIterator[bytes]:
    """Wrap an SSE byte stream, masking the model field of every data line."""
    buffer = b""
    async for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield rewrite_sse_line(line, public_model) + b"\n"
    if buffer:
        yield rewrite_sse_line(buffer, public_model)


def mask_json_model(body: dict[str, Any], public_model: str) -> dict[str, Any]:
    """Rewrite the model field of a non-streaming JSON response in place."""
    if isinstance(body, dict):
        body["model"] = public_model
    return body
