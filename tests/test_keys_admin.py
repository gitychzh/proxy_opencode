"""Tests for the dynamic key store, expiry auth, and admin endpoints."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from proxy_opencode.app import create_app
from proxy_opencode.auth.keystore import KeyStore
from proxy_opencode.config import Settings
from tests.test_gateway_routes import GW_HEADERS, FakeAdapter

ADMIN_HEADERS = {"Authorization": "Bearer api_ychzh22372222"}


def make_store(tmp_path, records=None) -> KeyStore:
    store = KeyStore(tmp_path / "keys.json")
    return store


def make_app(tmp_path, **overrides):
    base = dict(
        upstream_mode="opencode-serve",
        gateway_api_keys=["gw-key"],
        admin_api_keys=["api_ychzh22372222"],
        key_store_path=str(tmp_path / "keys.json"),
        opencode_server_password="pw",
    )
    base.update(overrides)
    app = create_app(Settings(**base))
    app.state.adapter = FakeAdapter()
    return app


# --------------------------------------------------------------- keystore


def test_create_default_ttl_24h(tmp_path):
    store = make_store(tmp_path)
    rec = store.create(name="t")
    assert rec.key.startswith("gw-")
    assert rec.is_valid()
    exp = rec.expiry()
    delta = exp - datetime.now(timezone.utc)
    assert timedelta(hours=23, minutes=58) < delta <= timedelta(hours=24)


def test_create_permanent_and_custom_ttl(tmp_path):
    store = make_store(tmp_path)
    perm = store.create(ttl_hours=0)
    assert perm.expires_at is None
    short = store.create(ttl_hours=1)
    assert short.expiry() - datetime.now(timezone.utc) <= timedelta(hours=1)


def test_expired_key_not_valid(tmp_path):
    store = make_store(tmp_path)
    rec = store.create(ttl_hours=1)
    # Force expiry by backdating.
    rec.expires_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    assert not rec.is_valid()
    assert store.validate(rec.key) is None
    assert store.lookup_expired(rec.key) is not None  # still auditable


def test_revocation_persists(tmp_path):
    store = make_store(tmp_path)
    rec = store.create()
    assert store.revoke(rec.id)
    assert store.validate(rec.key) is None
    # Reload from disk.
    store2 = KeyStore(tmp_path / "keys.json")
    assert store2.validate(rec.key) is None


def test_store_persists_across_reload(tmp_path):
    store = make_store(tmp_path)
    rec = store.create(name="persist")
    store2 = KeyStore(tmp_path / "keys.json")
    assert store2.validate(rec.key) is not None
    assert store2.list()[0].name == "persist"


# ------------------------------------------------------------------- auth


@pytest.mark.asyncio
async def test_dynamic_key_can_chat(tmp_path):
    app = make_app(tmp_path)
    rec = app.state.keystore.create(ttl_hours=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "ds41f_cus", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {rec.key}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_expired_key_rejected_401(tmp_path):
    app = make_app(tmp_path)
    rec = app.state.keystore.create(ttl_hours=1)
    rec.expires_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "ds41f_cus", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {rec.key}"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_key_rejected_401(tmp_path):
    app = make_app(tmp_path)
    rec = app.state.keystore.create(ttl_hours=1)
    app.state.keystore.revoke(rec.id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/models", headers={"Authorization": f"Bearer {rec.key}"}
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_key_is_permanent_and_can_chat(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "ds41f_cus", "messages": [{"role": "user", "content": "hi"}]},
            headers=ADMIN_HEADERS,
        )
    assert resp.status_code == 200


# ------------------------------------------------------------ admin API


@pytest.mark.asyncio
async def test_admin_create_list_revoke(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r1 = await client.post(
            "/admin/keys", json={"name": "phone"}, headers=ADMIN_HEADERS
        )
        assert r1.status_code == 200
        created = r1.json()
        assert created["key"].startswith("gw-")
        assert created["expires_at"] is not None  # default 24h

        # Minted key works for chat.
        r2 = await client.post(
            "/v1/chat/completions",
            json={"model": "ds41f_cus", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {created['key']}"},
        )
        assert r2.status_code == 200

        # Listing masks the key.
        r3 = await client.get("/admin/keys", headers=ADMIN_HEADERS)
        keys = r3.json()["keys"]
        assert keys and created["key"] not in json.dumps(r3.json())

        # Revoke -> chat fails.
        r4 = await client.delete(
            f"/admin/keys/{created['id']}", headers=ADMIN_HEADERS
        )
        assert r4.status_code == 200
        r5 = await client.post(
            "/v1/chat/completions",
            json={"model": "ds41f_cus", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {created['key']}"},
        )
        assert r5.status_code == 401


@pytest.mark.asyncio
async def test_admin_endpoints_reject_non_admin(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for headers in ({}, GW_HEADERS):
            r = await client.get("/admin/keys", headers=headers)
            assert r.status_code == 401
        r = await client.post("/admin/keys", json={}, headers=GW_HEADERS)
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_create_permanent_key(tmp_path):
    app = make_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/admin/keys", json={"ttl_hours": 0}, headers=ADMIN_HEADERS
        )
        assert r.status_code == 200
        assert r.json()["expires_at"] is None
