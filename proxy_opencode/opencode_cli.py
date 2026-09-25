"""opencode-cli upstream adapter: local-only, official CLI, restricted.

This adapter shells out to the official ``opencode`` CLI on the local machine.
It intentionally does NOT touch rate limits, quotas, fingerprints, or version
checks of the CLI — all of that stays under the control of the official
client. All subprocess calls use list form with a timeout; ``shell=True`` is
never used.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from typing import Any

from .config import Settings

logger = logging.getLogger("proxy_opencode.opencode_cli")


class CliError(Exception):
    """The opencode CLI is unavailable, timed out, or exited non-zero."""


class CliResult:
    def __init__(self, stdout: str, exit_code: int, latency_ms: float) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.latency_ms = latency_ms


def _base_argv(settings: Settings) -> list[str]:
    """OPENCODE_BIN may contain extra args (e.g. 'python fake.py' in tests)."""
    return shlex.split(settings.opencode_bin)


def _base_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    if settings.opencode_xdg_data_home:
        env["XDG_DATA_HOME"] = settings.opencode_xdg_data_home
    return env


def _run(argv: list[str], settings: Settings) -> CliResult:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.opencode_run_timeout_s,
            env=_base_env(settings),
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliError(f"opencode CLI not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(
            f"opencode CLI timed out after {settings.opencode_run_timeout_s}s"
        ) from exc
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "opencode-cli call",
        extra={
            "cmd": argv[1] if len(argv) > 1 else "",
            "exit_code": proc.returncode,
            "latency_ms": latency_ms,
        },
    )
    if proc.returncode != 0:
        raise CliError(f"opencode CLI exited with code {proc.returncode}")
    return CliResult(proc.stdout, proc.returncode, latency_ms)


def model_allowed(settings: Settings, model: str) -> bool:
    return any(
        model.startswith(p) for p in settings.opencode_allowed_model_prefixes
    )


def list_models(settings: Settings) -> list[str]:
    """Run the models command and return allow-listed model ids."""
    tokens = shlex.split(settings.opencode_models_cmd)
    argv = _base_argv(settings) + tokens[1:]
    result = _run(argv, settings)
    models: list[str] = []
    for line in result.stdout.splitlines():
        model = line.strip()
        if model and model_allowed(settings, model):
            models.append(model)
    return models


def build_prompt(messages: list[dict[str, Any]]) -> str:
    """Cautiously flatten text messages of known roles into a single prompt."""
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role not in ("system", "user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            # Keep only text parts; drop non-text (images/audio/...).
            content = "".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if not isinstance(content, str) or not content.strip():
            continue
        parts.append(f"{role}:\n{content}")
    if not parts:
        raise CliError("messages contain no usable text content")
    return "\n\n".join(parts)


def run_completion(settings: Settings, model: str, prompt: str) -> CliResult:
    if not model_allowed(settings, model):
        raise CliError(f"model {model!r} is not in the allowed prefixes")
    argv = _base_argv(settings) + ["run", "-m", model, prompt]
    return _run(argv, settings)


class ModelsCache:
    """TTL cache for the CLI models list."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._expires_at = 0.0
        self._models: list[str] = []

    def get(self) -> list[str]:
        now = time.monotonic()
        if now < self._expires_at:
            return self._models
        self._models = list_models(self._settings)
        self._expires_at = now + self._settings.models_cache_ttl_s
        return self._models
