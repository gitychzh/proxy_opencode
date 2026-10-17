"""Relay catalog + hidden /v1/models_cus endpoint contracts.

Contract under test (owner requirement, 2026-10-17):
* the gateway relays only the configured upstream models
  (OPENCODE_ZEN_MODELS, default big-pickle) — the "all free models"
  enumeration is gone;
* /v1/models_cus is the ONLY path listing the relayed raw ids, accepts
  Bearer OR ``?key=`` (browser friendly), 401 without a key, 404 outside
  zen-direct;
* the standard /v1/models keeps serving ONLY the masked public alias, and
  chat rejects raw upstream ids (404) — knowing big-pickle does not make
  it addressable.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings
from proxy_opencode.egress.catalog import ZenFreeCatalog, is_free_entry

ALIAS = "deepseek-ai/deepseek-v4.1-flash"

# Live-anonymous shape (2026-10-16): bare ids, no pricing fields. Upstream
# offers many free models; the gateway relays exactly one of them.
MOCK_UPSTREAM = {
    "object": "list",
    "data": [
        {"id": "space-bunny-free", "object": "model", "owned_by": "opencode"},
        {"id": "deepseek-v4.1-flash-free", "object": "model"},
        {"id": "big-pickle", "object": "model"},
        {"id": "test", "object": "model"},
        {"id": "claude-opus-5", "object": "model"},
    ],
}


# ------------------------------------------------------------- free filter


def test_is_free_entry_conventions():
    assert is_free_entry({"id": "big-pickle"})
    assert is_free_entry({"id": "cat-jam-free"})
    assert is_free_entry({"id": "deepseek-v4.1-flash-free"})
    # "test" and paid ids stay out — no suffix, no markers.
    assert not is_free_entry({"id": "test"})
    assert not is_free_entry({"id": "claude-opus-5"})
    assert not is_free_entry({"id": ""})
    assert not is_free_entry({"id": 42})
    assert not is_free_entry({})


def test_is_free_entry_explicit_markers_win():
    assert is_free_entry({"id": "x", "allowAnonymous": True})
    # The marker wins even when a cost field is (inconsistently) present.
    assert is_free_entry({"id": "x", "allowAnonymous": True, "cost": 3})
    assert is_free_entry({"id": "x", "cost": 0})
    assert not is_free_entry({"id": "x", "cost": 0.5})
    assert is_free_entry({"id": "x", "pricing": {"input": 0, "output": 0}})
    assert not is_free_entry({"id": "x", "pricing": {"input": "1.0", "output": 0}})
    # No pricing key -> convention decides even without markers.
    assert is_free_entry({"id": "x-free", "pricing": {}})


def test_catalog_host_check():
    with pytest.raises(ValueError):
        ZenFreeCatalog("https://evil.example.com/zen/v1")


# ------------------------------------------------------- relay allowlist


@pytest.mark.asyncio
async def test_catalog_lists_only_relayed_ids():
    cat = ZenFreeCatalog(
        "https://opencode.ai/zen/v1",
        relay_ids=["big-pickle"],
    )
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").respond(json=MOCK_UPSTREAM)
        assert await cat.refresh() is True
    # Every other free id upstream offers is filtered out.
    assert cat.ids() == ["big-pickle"]


@pytest.mark.asyncio
async def test_catalog_degraded_falls_back_to_static_relay_list():
    cat = ZenFreeCatalog(
        "https://opencode.ai/zen/v1",
        relay_ids=["big-pickle"],
    )
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").respond(status_code=503)
        assert await cat.refresh() is False
    assert cat.ids() == ["big-pickle"]
    assert cat.degraded is True
    body = cat.body()
    assert [m["id"] for m in body["data"]] == ["big-pickle"]
    assert body["metadata"]["stale"] is True
    metadata = body["metadata"]
    assert metadata["refreshed_at"] is None


# ---------------------------------------------------------- models_cus endpoint


def make_app(tmp_path, mode: str = "zen-direct", **kw):
    settings = Settings(
        upstream_mode=mode,
        gateway_api_keys=["gw-key"],
        admin_api_keys=["adm-key"],
        key_store_path=str(tmp_path / "keys.json"),
        **kw,
    )
    app = create_app(settings)
    # The lifespan-probe loop is not started under ASGITransport; refresh
    # synchronously against the mocked upstream instead.
    return app


@pytest.mark.asyncio
async def test_models_cus_default_is_plain_bare_ids(tmp_path):
    """Default body = one bare id per line, nothing else (owner 2026-10-16)."""

    app = make_app(tmp_path)
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").respond(json=MOCK_UPSTREAM)
        await app.state.free_catalog.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/v1/models_cus?key=gw-key")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "big-pickle"


@pytest.mark.asyncio
async def test_models_cus_format_json_keeps_openai_shape(tmp_path):
    app = make_app(tmp_path)
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").respond(json=MOCK_UPSTREAM)
        await app.state.free_catalog.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get(
                "/v1/models_cus", params={"key": "gw-key", "format": "json"}
            )
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["id"] for m in body["data"]]
    assert ids == ["big-pickle"]
    assert body["metadata"]["source"] == "opencode-zen relayed models"
    assert body["metadata"]["stale"] is False


@pytest.mark.asyncio
async def test_models_cus_accepts_bearer_and_rejects_missing_key(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ok = await client.get(
            "/v1/models_cus", headers={"Authorization": "Bearer gw-key"}
        )
        denied = await client.get("/v1/models_cus")
        bad = await client.get("/v1/models_cus?key=wrong-key")
        admin = await client.get("/v1/models_cus?key=adm-key")
    assert ok.status_code == 200
    assert denied.status_code == 401
    assert bad.status_code == 401
    assert json.loads(denied.text)["error"]["type"] == "authentication_error"
    assert admin.status_code == 200  # admin keys double as gateway keys


@pytest.mark.asyncio
async def test_models_cus_hidden_and_not_in_models(tmp_path):
    """Standard /v1/models must NOT enumerate raw ids — models_cus only."""
    app = make_app(tmp_path)
    with respx.mock:
        respx.get("https://opencode.ai/zen/v1/models").respond(json=MOCK_UPSTREAM)
        await app.state.free_catalog.refresh()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            models = await client.get(
                "/v1/models", headers={"Authorization": "Bearer gw-key"}
            )
    data = models.json()["data"]
    ids = [m["id"] for m in data]
    assert ids == [ALIAS]  # exactly the public alias, nothing else
    assert "big-pickle" not in ids
    assert "space-bunny-free" not in ids
    # Strictly standard OpenAI model shape (hermes discovery).
    assert set(data[0]) == {"id", "object", "created", "owned_by"}


@pytest.mark.asyncio
async def test_raw_upstream_id_rejected_404(tmp_path):
    """Knowing the relayed raw id (from /v1/models_cus) does not make it
    addressable: only the public alias is — the backend name stays hidden."""
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "big-pickle",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": "Bearer gw-key"},
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_models_cus_404_outside_zen_direct(tmp_path):
    app = make_app(tmp_path, mode="opencode-serve", opencode_server_password="pw")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/v1/models_cus?key=gw-key")
    assert resp.status_code == 404
