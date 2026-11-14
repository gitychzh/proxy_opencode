"""Free-model snapshot, daily sync and verbatim passthrough contracts.

The gateway must publish every opencode free bare id while keeping the legacy
public alias working, and the host-side sync job must write nothing at all
when the upstream catalogue has not changed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from proxy_opencode.app import create_app
from proxy_opencode.config import Settings
from proxy_opencode.egress.free_models import (
    ENV_KEY,
    FreeModelsSnapshot,
    fetch_free_ids,
    read_snapshot_ids,
    sync_free_models,
    update_env_key,
    write_snapshot,
)
from proxy_opencode.registry import ModelRegistry, UnknownModelError
from proxy_opencode.routes._pipeline import new_call

ALIAS = "deepseek-ai/deepseek-v4.1-flash"
FREE_IDS = ["big-pickle", "glm-5.3-flash-free", "mimo-v2.6-flash-free"]

UPSTREAM = {
    "object": "list",
    "data": [
        # The live anonymous view nests cost, and paid entries are zero too.
        {"id": "big-pickle", "cost": {"text": {"output": {"price": 0}}}},
        {"id": "glm-5.3-flash-free"},
        {"id": "mimo-v2.6-flash-free"},
        {"id": "paid-model", "cost": {"text": {"output": {"price": 0}}}},
    ],
}


def _fetch(_url: str, _headers: dict[str, str], _timeout: float) -> bytes:
    return json.dumps(UPSTREAM).encode("utf-8")


def _bump(path: Path) -> None:
    """Force a distinct mtime so mtime-based reload is deterministic."""

    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))


# ------------------------------------------------------------- fetch / filter


def test_fetch_free_ids_uses_naming_convention():
    assert fetch_free_ids(fetch=_fetch) == sorted(FREE_IDS)


def test_fetch_free_ids_avoids_python_urllib_403():
    """Cloudflare Error 1010 blocks the default Python UA on /models."""

    seen: dict[str, str] = {}

    def fetch(url: str, headers: dict[str, str], timeout: float) -> bytes:
        seen.update(headers)
        return _fetch(url, headers, timeout)

    fetch_free_ids(fetch=fetch)
    assert "User-Agent" in seen
    assert "Python-urllib" not in seen["User-Agent"]


def test_fetch_free_ids_rejects_bad_shape():
    with pytest.raises(ValueError):
        fetch_free_ids(fetch=lambda *_: b'{"object":"list"}')
    with pytest.raises(ValueError):
        fetch_free_ids(fetch=lambda *_: b"not json")
    assert fetch_free_ids(fetch=lambda *_: b'{"data": []}') == []


# ----------------------------------------------------------------- snapshot


def test_snapshot_hot_reloads_corruption_and_deletion(tmp_path):
    state = tmp_path / "state.json"
    assert FreeModelsSnapshot(state).ids() == []
    write_snapshot(state, FREE_IDS)
    snap = FreeModelsSnapshot(state)
    assert snap.ids() == sorted(FREE_IDS)
    assert snap.updated_at
    write_snapshot(state, ["big-pickle", "new-free"])
    _bump(state)
    assert snap.ids() == ["big-pickle", "new-free"]
    state.write_text("{broken", encoding="utf-8")
    _bump(state)
    assert snap.ids() == []
    state.unlink()
    assert snap.ids() == []


def test_write_snapshot_is_atomic_json(tmp_path):
    state = tmp_path / "nested" / "state.json"
    body = write_snapshot(state, FREE_IDS, updated_at="2026-11-14T22:00:00+00:00")
    assert body["ids"] == sorted(FREE_IDS)
    assert json.loads(state.read_text(encoding="utf-8")) == body
    assert not list(state.parent.glob(".state.json.*"))


# --------------------------------------------------------------- env rewrite


def test_update_env_key_preserves_comments_and_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment OPENCODE_ZEN_MODELS=old\n"
        "OTHER=keep\n"
        "OPENCODE_ZEN_MODELS=big-pickle\n"
        "TAIL=yes\n",
        encoding="utf-8",
    )
    assert update_env_key(env, ENV_KEY, "a-free,b-free") is True
    text = env.read_text(encoding="utf-8")
    assert "# comment OPENCODE_ZEN_MODELS=old" in text
    assert "OTHER=keep" in text and "TAIL=yes" in text
    assert "OPENCODE_ZEN_MODELS=a-free,b-free" in text
    assert update_env_key(env, ENV_KEY, "a-free,b-free") is False


def test_update_env_key_appends_when_missing_and_keeps_crlf(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"A=1\r\nOPENCODE_ZEN_MODELS=old\r\n")
    assert update_env_key(env, ENV_KEY, "x-free")
    assert env.read_bytes() == b"A=1\r\nOPENCODE_ZEN_MODELS=x-free\r\n"
    missing = tmp_path / "fresh.env"
    missing.write_bytes(b"A=1\n")
    assert update_env_key(missing, ENV_KEY, "x-free")
    assert missing.read_bytes() == b"A=1\nOPENCODE_ZEN_MODELS=x-free\n"


# ------------------------------------------------------------------- sync


def test_sync_unchanged_writes_nothing(tmp_path):
    state = tmp_path / "state.json"
    env = tmp_path / ".env"
    env.write_text(f"{ENV_KEY}=stale\n", encoding="utf-8")
    write_snapshot(state, sorted(FREE_IDS))
    state_mtime = state.stat().st_mtime_ns
    result = sync_free_models(state_file=state, env_file=env, fetch=_fetch)
    assert result.changed is False
    assert result.reason == "unchanged"
    assert result.state_written is False and result.env_written is False
    assert state.stat().st_mtime_ns == state_mtime
    assert env.read_text(encoding="utf-8") == f"{ENV_KEY}=stale\n"


def test_sync_first_run_writes_snapshot_and_env(tmp_path):
    state = tmp_path / "data" / "state.json"
    env = tmp_path / ".env"
    env.write_text(f"{ENV_KEY}=big-pickle\nOTHER=1\n", encoding="utf-8")
    result = sync_free_models(state_file=state, env_file=env, fetch=_fetch)
    assert result.changed and result.reason == "first-run"
    assert result.ids == sorted(FREE_IDS)
    assert read_snapshot_ids(state) == sorted(FREE_IDS)
    text = env.read_text(encoding="utf-8")
    assert f"{ENV_KEY}={','.join(sorted(FREE_IDS))}" in text
    assert "OTHER=1" in text


def test_sync_changed_reports_diff_and_dry_run_writes_nothing(tmp_path):
    state = tmp_path / "state.json"
    write_snapshot(state, ["big-pickle", "gone-free"])
    env = tmp_path / ".env"
    env.write_text(f"{ENV_KEY}=big-pickle\n", encoding="utf-8")
    before = (state.stat().st_mtime_ns, env.read_text(encoding="utf-8"))
    dry = sync_free_models(
        state_file=state, env_file=env, dry_run=True, fetch=_fetch
    )
    assert dry.changed and dry.state_written is False
    assert dry.added == ["glm-5.3-flash-free", "mimo-v2.6-flash-free"]
    assert dry.removed == ["gone-free"]
    assert (state.stat().st_mtime_ns, env.read_text(encoding="utf-8")) == before
    real = sync_free_models(state_file=state, env_file=env, fetch=_fetch)
    assert real.changed and real.env_written
    assert read_snapshot_ids(state) == sorted(FREE_IDS)


def test_sync_refuses_an_empty_upstream_catalogue(tmp_path):
    state = tmp_path / "state.json"
    write_snapshot(state, FREE_IDS)
    with pytest.raises(ValueError):
        sync_free_models(state_file=state, fetch=lambda *_: b'{"data": []}')
    assert read_snapshot_ids(state) == sorted(FREE_IDS)


# --------------------------------------------------------------- registry


def _registry(tmp_path, ids=None):
    state = tmp_path / "state.json"
    if ids is not None:
        write_snapshot(state, ids)
    snap = FreeModelsSnapshot(state)
    settings = Settings(zen_free_models_file=str(state))
    return ModelRegistry(settings, snap), state, settings


def test_registry_without_snapshot_keeps_legacy_contract():
    reg = ModelRegistry(Settings())
    assert [m["id"] for m in reg.catalog()["data"]] == [ALIAS]
    with pytest.raises(UnknownModelError):
        reg.resolve("big-pickle")


def test_registry_publishes_alias_plus_free_ids(tmp_path):
    reg, _, _ = _registry(tmp_path, FREE_IDS)
    data = reg.catalog()["data"]
    assert data[0]["id"] == ALIAS
    assert {m["id"] for m in data[1:]} == set(FREE_IDS)
    assert all(set(m) == {"id", "object", "created", "owned_by"} for m in data)


def test_registry_free_ids_are_verbatim_and_echoed_unchanged(tmp_path):
    reg, _, _ = _registry(tmp_path, FREE_IDS)
    for mid in FREE_IDS:
        assert reg.resolve(mid) == mid
        assert reg.public_id(mid) == mid
    assert reg.resolve(ALIAS) == "big-pickle"
    assert reg.public_id(ALIAS) == ALIAS
    assert reg.public_id("unknown-upstream-echo") == ALIAS


def test_registry_rejects_ids_outside_the_snapshot(tmp_path):
    reg, _, _ = _registry(tmp_path, FREE_IDS)
    for raw in ("gpt-4o", "opencode/big-pickle", "", None):
        with pytest.raises(UnknownModelError):
            reg.resolve(raw)


def test_registry_follows_a_snapshot_rewrite_without_a_restart(tmp_path):
    reg, state, _ = _registry(tmp_path, FREE_IDS)
    assert reg.resolve("mimo-v2.6-flash-free") == "mimo-v2.6-flash-free"
    write_snapshot(state, ["big-pickle", "new-free"])
    _bump(state)
    assert reg.resolve("new-free") == "new-free"
    assert reg.public_id("new-free") == "new-free"
    with pytest.raises(UnknownModelError):
        reg.resolve("mimo-v2.6-flash-free")


def test_new_call_keeps_free_id_on_both_legs(tmp_path):
    reg, _, settings = _registry(tmp_path, FREE_IDS)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )
    call = new_call(
        request,
        {"model": "glm-5.3-flash-free", "messages": [], "stream": True},
        settings,
        reg,
    )
    assert call.upstream_model == "glm-5.3-flash-free"
    assert call.public_model == "glm-5.3-flash-free"


# ------------------------------------------------------------ HTTP surface


@pytest.mark.asyncio
async def test_models_endpoint_publishes_every_free_id(tmp_path):
    state = tmp_path / "state.json"
    write_snapshot(state, FREE_IDS)
    settings = Settings(
        zen_free_models_file=str(state),
        gateway_api_keys=["gw-key"],
        admin_api_keys=["adm-key"],
        key_store_path=str(tmp_path / "keys.json"),
    )
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/models", headers={"Authorization": "Bearer gw-key"}
        )
    ids = [m["id"] for m in resp.json()["data"]]
    assert resp.status_code == 200
    assert ids[0] == ALIAS
    assert set(ids[1:]) == set(FREE_IDS)


# ------------------------------------------------------------ CLI drift guard


def test_client_version_drift_flags_mismatch(monkeypatch):
    """npm latest != pin -> drift=True and a loud payload (protocol guard)."""


    from scripts.sync_zen_free_models import check_client_version_drift

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"version": "1.20.0"}'

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout: FakeResponse()
    )
    info = check_client_version_drift()
    assert info["pinned"] == "1.19.5"
    assert info["latest"] == "1.20.0"
    assert info["drift"] is True


def test_client_version_drift_survives_registry_failure(monkeypatch):
    """A registry outage must not fail the sync (warning-only guard)."""

    import urllib.error

    from scripts.sync_zen_free_models import check_client_version_drift

    def boom(req, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    info = check_client_version_drift()
    assert info["drift"] is False
    assert info["latest"] is None
    assert info["error"] == "URLError"
