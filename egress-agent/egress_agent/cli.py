"""Command line interface: ``egress-agent [--config PATH] <run|status>``.

P0 status: config loading + ``status`` + logging wiring are real and tested;
``run`` (the data plane) and ``rotate`` land in P1 with tests of their own.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import AgentConfig, ConfigError
from .logging_setup import setup_logging

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_NOT_IMPLEMENTED = 3


def build_parser() -> argparse.ArgumentParser:
    # Shared options accepted both before and after the subcommand: a parent
    # parser with SUPPRESS defaults keeps the top-level values intact when the
    # sub-level flag is absent, so `--config X status` and `status --config X`
    # behave identically.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        action="store",
        default=argparse.SUPPRESS,
        help="path to the JSON config file (default: %(default)s)",
    )
    common.add_argument(
        "--log-level", action="store", default=argparse.SUPPRESS,
        help="log level (default: %(default)s)",
    )

    parser = argparse.ArgumentParser(
        prog="egress-agent",
        description="Device-side egress proxy agent for the proxy_opencode stack.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default="/etc/egress-agent/agent.json",
        help="path to the JSON config file (default: %(default)s)",
    )
    parser.add_argument("--log-level", default="INFO", help="log level (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common],
                         help="start the proxy + health + control plane (P1)")
    run.add_argument("--check", action="store_true", help="validate config and exit (P0)")

    status = sub.add_parser("status", parents=[common],
                            help="print the effective, secret-free config summary")
    status.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    rotate = sub.add_parser("rotate", parents=[common],
                            help="reboot the configured ONT to renew the egress IP (P1)")
    rotate.add_argument("--ont", default=None, help="ONT name from the config (default: all)")
    rotate.add_argument("--wait", action="store_true", help="wait for the egress to come back")
    return parser


def _load(args: argparse.Namespace) -> AgentConfig:
    return AgentConfig.load(Path(args.config))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)

    if args.command == "status":
        try:
            config = _load(args)
        except ConfigError as exc:
            print(f"egress-agent: config error: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        summary = config.summary()
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"egress     : {summary['egress_name']}")
            print(f"bind_source: {summary['bind_source'] or '(OS routing)'}")
            print(f"listen     : {summary['listen']}")
            print(f"proxy auth : {'yes' if summary['proxy_auth'] else 'no'}")
            print(f"echo_url   : {summary['echo_url']}")
            for ont in summary["onts"]:
                iface = f" via {ont['interface']}" if ont["interface"] else ""
                print(f"ont        : {ont['name']} ({ont['kind']}) {ont['url']}{iface}"
                      f" user={ont['username']} enabled={ont['enabled']}")
        return EXIT_OK

    if args.command == "run":
        try:
            config = _load(args)
        except ConfigError as exc:
            print(f"egress-agent: config error: {exc}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        if args.check:
            print(json.dumps({"ok": True, "config": config.summary()}, ensure_ascii=False))
            return EXIT_OK
        print(
            "egress-agent: the data plane (proxy/health/rotate) lands in P1",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED

    if args.command == "rotate":
        print(
            "egress-agent: rotate lands in P5 (ONT driver exists from P1)",
            file=sys.stderr,
        )
        return EXIT_NOT_IMPLEMENTED

    parser.error(f"unknown command {args.command!r}")
    return EXIT_CONFIG_ERROR  # pragma: no cover - argparse exits


if __name__ == "__main__":  # pragma: no cover
        raise SystemExit(main())
