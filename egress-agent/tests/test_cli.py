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


def test_run_without_check_reports_p1(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_NOT_IMPLEMENTED, main

    rc = main(["--config", _config(tmp_path), "run"])
    err = capsys.readouterr().err
    assert rc == EXIT_NOT_IMPLEMENTED
    assert "P1" in err


def test_rotate_reports_p1(tmp_path, capsys) -> None:
    from egress_agent.cli import EXIT_NOT_IMPLEMENTED, main

    rc = main(["--config", _config(tmp_path), "rotate"])
    err = capsys.readouterr().err
    assert rc == EXIT_NOT_IMPLEMENTED
    assert "P5" in err


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
