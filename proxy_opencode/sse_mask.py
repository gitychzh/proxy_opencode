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


def rewrite_sse_line(
    line: bytes,
    public_model: str,
    patterns: list[tuple[re.Pattern[bytes], bytes]] | None = None,
) -> bytes:
    """Rewrite the model / leaked-name / renamed-tool fields of one SSE line."""
    if not line.startswith(b"data:"):
        return line
    if b'"model"' in line:
        line = _MODEL_FIELD.sub(rb"\g<1>" + public_model.encode("utf-8") + rb"\g<2>", line)
    if b'"name"' in line:
        for pat, repl in patterns or ():
            if pat.search(line):
                line = pat.sub(repl, line)
        line = _scrub_leaked_name(line)
    return line


def _restore_patterns(mapping: dict[str, str]) -> list[tuple[re.Pattern[bytes], bytes]]:
    """Byte patterns matching ``"name": "client_<x>"`` for each renamed tool.

    Only the ``name`` field is touched: the value is what the body gate
    rewrote, so this is the exact inverse and cannot collide with ordinary
    content (which would have to contain this literal JSON fragment).
    """

    out: list[tuple[re.Pattern[bytes], bytes]] = []
    for new, orig in mapping.items():
        pat = re.compile(rb'("name"\s*:\s*")' + re.escape(new.encode("utf-8")) + rb'(")')
        out.append((pat, rb"\g<1>" + orig.encode("utf-8") + rb"\g<2>"))
    return out


async def mask_model_field(
    chunks: AsyncIterator[bytes],
    public_model: str,
    restore: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Wrap an SSE byte stream, masking the model field of every data line.

    ``restore`` is this request's tool-rename reverse map (see
    ``tools.rename_collisions``); when present, every streamed tool-call name
    is put back to the name the client actually declared.

    The wrapped stream is closed deterministically on teardown (client
    disconnect closes this generator, which raises ``GeneratorExit`` at the
    ``yield``): without an explicit ``aclose`` the upstream response — and,
    in 0.7.x, the egress-pool lease it holds — would only be released when
    the garbage collector got to it.
    """

    patterns = _restore_patterns(restore or {})

    buffer = b""
    try:
        async for chunk in chunks:
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield rewrite_sse_line(line, public_model, patterns) + b"\n"
        if buffer:
            yield rewrite_sse_line(buffer, public_model, patterns)
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # pragma: no cover - teardown must not mask exit
                pass


def mask_json_model(
    body: dict[str, Any],
    public_model: str,
    restore: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Rewrite the model field of a non-streaming JSON response in place.

    Also drops a leaked `message.name` (same signature as the SSE leak) so
    the upstream model identity never reaches the client in either shape, and
    restores any renamed tool-call names to the ones the client declared.
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
        if restore:
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict) and fn.get("name") in restore:
                    fn["name"] = restore[fn["name"]]
    return body
