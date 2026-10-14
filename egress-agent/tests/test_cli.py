"""Tests for the egress-agent CLI (P0 scope)."""

from __future__ import annotations

import json


def _config(tmp_path) -> str:
    p = tmp_path / "agent.json"
    p.write_text(
        json.dumps(
            {
                "listener": {"port": 10001},
                "egress": {"name": "opc2-wifi"},
                "onts": [{"name": "hg8020c", "interface": "wlx0c9160fb3c7c"}],
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def test_status_human_readable(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_OK, main

    rc = main(["--config", _config(tmp_path), "status"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "opc2-wifi" in out
    assert "hg8020c" in out
    assert "wlx0c9160fb3c7c" in out


def test_status_json(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_OK, main

    rc = main(["--config", _config(tmp_path), "status", "--json"])
    out = capsys.readouterr().out
    assert rc == EXIT_OK
    payload = json.loads(out)
    assert payload["egress_name"] == "opc2-wifi"
    assert payload["listen"] == "0.0.0.0:10001"
    assert payload["onts"][0]["name"] == "hg8020c"


def test_run_check_mode_validates_config(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_OK, main

    rc = main(["--config", _config(tmp_path), "run", "--check"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT_OK
    assert payload["ok"] is True
    assert payload["config"]["egress_name"] == "opc2-wifi"


def test_rotate_reports_driver_result(tmp_path, capsys, monkeypatch) -> None:
    from egress_agent.cli import EXIT_OK, main
    from egress_agent.rotate import RotationResult

    async def fake_restart(ont, *, wait=False):
        return RotationResult(ok=True, ont=ont.name, detail="mock")

    monkeypatch.setattr("egress_agent.rotate.restart_ont", fake_restart)
    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({
        "egress": {"name": "opc2-wifi"},
        "onts": [{"name": "hg8020c"}],
    }), encoding="utf-8")
    rc = main(["--config", str(cfg), "rotate", "--ont", "hg8020c"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == EXIT_OK
    assert payload["ok"] is True
    assert payload["results"][0]["ont"] == "hg8020c"


def test_rotate_unknown_ont_exit_code(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_CONFIG_ERROR, main

    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({
        "egress": {"name": "opc2-wifi"},
        "onts": [{"name": "hg8020c"}],
    }), encoding="utf-8")
    rc = main(["--config", str(cfg), "rotate", "--ont", "nope"])
    assert rc == EXIT_CONFIG_ERROR
    assert "unknown ont" in capsys.readouterr().out


def test_bad_config_exit_code(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_CONFIG_ERROR, main

    rc = main(["--config", str(tmp_path / "missing.json"), "status"])
    assert rc == EXIT_CONFIG_ERROR
    assert "config error" in capsys.readouterr().err


def test_missing_subcommand_is_usage_error() -> None:
    import pytest

    from egress_agent.cli import main

    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_config_flag_accepted_after_subcommand(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_OK, main

    rc = main(["status", "--config", _config(tmp_path), "--json"])
    assert rc == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["egress_name"] == "opc2-wifi"


def test_build_agent_serves_healthz(tmp_path) -> None:
    """The running agent answers /healthz; teardown closes cleanly."""

    import asyncio

    import aiohttp

    from egress_agent.cli import build_agent
    from egress_agent.config import AgentConfig

    cfg = tmp_path / "agent.json"
    cfg.write_text(json.dumps({
        "listener": {"host": "127.0.0.1", "port": 0},
        "egress": {"name": "run-test"},
        "echo_url": "http://127.0.0.1:1/echo",
    }), encoding="utf-8")

    async def scenario() -> None:
        server, state, probe = await build_agent(AgentConfig.load(str(cfg)))
        try:
            assert server.port > 0
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{server.port}/healthz"
                ) as resp:
                    assert resp.status == 200
                    payload = await resp.json()
                    assert payload["egress"] == "run-test"
            assert state.egress_name == "run-test"
        finally:
            probe.cancel()
            await server.close()

    asyncio.run(scenario())
