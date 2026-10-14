"""JSON configuration for one egress-agent instance.

Config file format (JSON, stdlib-only parsing — no YAML dependency on
devices)::

    {
      "listener": {"host": "0.0.0.0", "port": 10001,
                   "username": null, "password": null},
      "egress":   {"name": "opc2-wifi", "bind_source": null},
      "echo_url": "https://myip.ipip.net",
      "onts": [
        {"name": "hg8020c", "kind": "huawei", "url": "http://192.168.1.1",
         "interface": "wlx0c9160fb3c7c",
         "username": "useradmin", "password": "REPLACED_ON_DEVICE"}
      ]
    }

Semantics:

* One agent instance fronts exactly ONE egress path (one uplink). Devices
  with two uplinks (opc2: wifi + eth) run two instances — see the deploy
  templates and docs/ARCHITECTURE.md.
* ``egress.bind_source``: optional source IP pinned on outbound sockets so
  traffic leaves through the intended interface even when the OS default
  route points elsewhere (Linux). ``null`` = OS routing decides.
* ``onts[].interface``: source interface used to REACH the ONT console. On
  opc2 the wired and wireless ONTs share 192.168.1.1, so the interface is
  the only reliable discriminator.
* ``onts[].password`` is secret: never commit the real file, and
  :meth:`AgentConfig.summary` redacts it for ``egress-agent status``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ECHO_URL = "https://myip.ipip.net"


class ConfigError(ValueError):
    """Raised when a config file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class ListenerConfig:
    """Where the agent listens for gateway CONNECT requests."""

    host: str = "0.0.0.0"
    port: int = 10001
    username: str | None = None
    password: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(f"listener.port out of range: {self.port}")
        if (self.username is None) != (self.password is None):
            raise ConfigError("listener.username/password must be set together")


@dataclass(frozen=True)
class EgressConfig:
    """The single uplink this instance fronts."""

    name: str = "default"
    bind_source: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigError("egress.name must be non-empty")


@dataclass(frozen=True)
class OntConfig:
    """One controllable ONT/router used for IP rotation."""

    name: str
    kind: str = "huawei"
    url: str = "http://192.168.1.1"
    interface: str | None = None
    username: str = "user"
    password: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ConfigError("onts[].name must be non-empty")
        if not self.url.startswith(("http://", "https://")):
            raise ConfigError(f"onts[{self.name}].url must be http(s): {self.url}")

    def redacted(self) -> dict[str, Any]:
        """Secret-free view for logs and ``status`` output."""
        return {
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "interface": self.interface,
            "username": self.username,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class AgentConfig:
    """Everything one agent instance needs."""

    listener: ListenerConfig = field(default_factory=ListenerConfig)
    egress: EgressConfig = field(default_factory=EgressConfig)
    echo_url: str = DEFAULT_ECHO_URL
    onts: tuple[OntConfig, ...] = ()

    def __post_init__(self) -> None:
        names = [ont.name for ont in self.onts]
        if len(names) != len(set(names)):
            raise ConfigError(f"duplicate onts[].name: {names}")
        if self.echo_url and not self.echo_url.startswith(("http://", "https://")):
            raise ConfigError(f"echo_url must be http(s): {self.echo_url}")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> AgentConfig:
        if not isinstance(data, dict):
            raise ConfigError("config root must be a JSON object")
        listener = ListenerConfig(**(data.get("listener") or {}))
        egress = EgressConfig(**(data.get("egress") or {}))
        raw_onts = data.get("onts") or []
        if not isinstance(raw_onts, list):
            raise ConfigError("onts must be a list")
        onts = tuple(OntConfig(**item) for item in raw_onts)
        return cls(
            listener=listener,
            egress=egress,
            echo_url=str(data.get("echo_url") or DEFAULT_ECHO_URL),
            onts=onts,
        )

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        p = Path(path)
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {p}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config {p} is not valid JSON: {exc}") from exc
        return cls.from_mapping(data)

    def summary(self) -> dict[str, Any]:
        """Secret-free summary for ``egress-agent status`` and logs."""
        return {
            "egress_name": self.egress.name,
            "bind_source": self.egress.bind_source,
            "listen": f"{self.listener.host}:{self.listener.port}",
            "proxy_auth": bool(self.listener.username),
            "echo_url": self.echo_url,
            "onts": [ont.redacted() for ont in self.onts],
        }
