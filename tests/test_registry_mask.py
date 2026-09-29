"""Tests for the model alias registry and SSE model masking."""

from __future__ import annotations

import json

from proxy_opencode.config import Settings
from proxy_opencode.registry import ModelRegistry
from proxy_opencode.sse_mask import mask_model_field, rewrite_sse_line


def make_registry(**kw) -> ModelRegistry:
    return ModelRegistry(Settings(**kw))


# ---------------------------------------------------------------- registry


def test_default_catalogue_has_single_public_model():
    reg = make_registry()
    assert reg.enabled
    cat = reg.catalog()
    ids = [m["id"] for m in cat["data"]]
    assert ids == ["ds41f_cus"]
    assert cat["data"][0]["display_name"] == "DeepSeek V4.1 Flash"


def test_resolve_maps_any_requested_model_to_upstream():
    reg = make_registry()
    assert reg.resolve("ds41f_cus") == "opencode/big-pickle"
    # Unknown ids (incl. direct upstream naming attempts) resolve transparently.
    assert reg.resolve("opencode/big-pickle") == "opencode/big-pickle"
    assert reg.resolve("gpt-4o") == "opencode/big-pickle"
    assert reg.resolve("") == "opencode/big-pickle"
    assert reg.resolve(None) == "opencode/big-pickle"


def test_public_id_masks_every_upstream_echo():
    reg = make_registry()
    assert reg.public_id("big-pickle") == "ds41f_cus"
    assert reg.public_id("opencode/big-pickle") == "ds41f_cus"
    assert reg.public_id("whatever-upstream-echoes") == "ds41f_cus"
    assert reg.public_id("ds41f_cus") == "ds41f_cus"


def test_mask_off_is_passthrough():
    reg = make_registry(mask_models=False)
    assert not reg.enabled
    assert reg.resolve("gpt-4o") == "gpt-4o"
    assert reg.public_id("big-pickle") == "big-pickle"


def test_custom_catalogue_entries():
    reg = ModelRegistry(
        Settings(
            public_models=[
                {"id": "a", "display_name": "A", "upstream": "up-a"},
                {"id": "b", "display_name": "B", "upstream": "up-b"},
            ]
        )
    )
    assert reg.resolve("b") == "up-b"
    assert reg.public_id("up-a") == "a"


# --------------------------------------------------------------- sse mask


def test_rewrite_sse_line_masks_model_field():
    line = b'data: {"id":"x","model":"opencode/big-pickle","choices":[]}'
    out = rewrite_sse_line(line, "ds41f_cus")
    assert b'"model":"ds41f_cus"' in out
    assert b"big-pickle" not in out


def test_rewrite_sse_line_ignores_non_model_lines():
    line = b": keep-alive"
    assert rewrite_sse_line(line, "ds41f_cus") == line
    line2 = b"data: {\"choices\":[]}"
    assert rewrite_sse_line(line2, "ds41f_cus") == line2


async def _collect(chunks):
    out = b""
    async for c in mask_model_field(_async_iter(chunks), "ds41f_cus"):
        out += c
    return out


def _async_iter(items):
    async def gen():
        for i in items:
            yield i

    return gen()


async def test_mask_handles_lines_split_across_chunks():
    # One JSON payload deliberately split across two TCP chunks.
    part1 = b'data: {"id":"x","mod'
    part2 = b'el":"opencode/big-pickle","choices":[]}\n\n'
    out = await _collect([part1, part2])
    payload = json.loads(out.split(b"\n")[0][5:])
    assert payload["model"] == "ds41f_cus"


async def test_mask_passes_through_plain_lines():
    out = await _collect([b"data: [DONE]\n\n"])
    assert out == b"data: [DONE]\n\n"
