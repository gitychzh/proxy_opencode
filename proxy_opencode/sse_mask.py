"""SSE stream rewriting: scrub upstream model names out of relayed chunks.

Two leaks are handled here:

1. `"model": "<id>"` at the top level of every chunk — rewritten to the
   public model id.
2. `delta.name` — Zen puts the *upstream model's human-readable name* there
   (observed live 2026-09-30: `{"role":"assistant","content":"OK","name":"Space Bunny"}`).
   That field is not part of the OpenAI chat-delta contract, and leaving it
   in defeats model masking just as surely as an unmasked `model` field.

Both are rewritten at the byte level (no JSON re-serialisation) so the
passthrough stays faithful, and line-buffered so payloads split across TCP
chunks are handled correctly.
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator

_MODEL_FIELD = re.compile(rb'("model"\s*:\s*")[^"]*(")')

# A tool/function name can never contain whitespace, so a `name` value with a
# space in it is always the leaked upstream model display name. The first two
# patterns drop the key together with one separating comma so the JSON stays
# valid; the third handles the key being the ONLY member of its object
# (`{"name":"Space Bunny"}`) by emptying the value instead — removal there
# would leave `{}` anyway, and emptying is always shape-safe.
_LEAKY_NAME_TRAILING = re.compile(rb',\s*"name"\s*:\s*"[^"]*\s[^"]*"')
_LEAKY_NAME_LEADING = re.compile(rb'"name"\s*:\s*"[^"]*\s[^"]*"\s*,')
_LEAKY_NAME_BARE = re.compile(rb'"name"\s*:\s*"[^"]*\s[^"]*"')


def _scrub_leaked_name(line: bytes) -> bytes:
    line = _LEAKY_NAME_TRAILING.sub(b"", line)
    line = _LEAKY_NAME_LEADING.sub(b"", line)
    return _LEAKY_NAME_BARE.sub(b'"name": ""', line)


def rewrite_sse_line(line: bytes, public_model: str) -> bytes:
    """Rewrite the model / leaked-name fields of one SSE data line."""
    if not line.startswith(b"data:"):
        return line
    if b'"model"' in line:
        line = _MODEL_FIELD.sub(
            rb"\g<1>" + public_model.encode("utf-8") + rb"\g<2>", line
        )
    if b'"name"' in line:
        line = _scrub_leaked_name(line)
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
    """Rewrite the model field of a non-streaming JSON response in place.

    Also drops a leaked `message.name` (same signature as the SSE leak) so
    the upstream model identity never reaches the client in either shape.
    """
    if not isinstance(body, dict):
        return body
    body["model"] = public_model
    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        name = message.get("name")
        if isinstance(name, str) and " " in name:
            message.pop("name", None)
    return body
