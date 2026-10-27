#!/usr/bin/env bash
# Install egress-agent on an Ubuntu/device host (idempotent).
#
# Usage:
#   sudo ./install.sh <instance> [--user <user>] [--port <port>] \
#        [--python <interpreter>] [--install-dir <dir>] [--pip-index <url>]
#
# What it does (nothing else, nothing destructive):
#   1. create a dedicated venv at <install-dir>/venv (default /opt/egress-agent)
#   2. pip-install the agent + deps into that venv
#   3. install the systemd template unit
#   4. ensure /etc/egress-agent + /var/log/egress-agent exist
#   5. print the next manual step: drop the real config and enable the unit
#
# Why a venv under /opt instead of the system python: the host python may be
# older than 3.12 (the project requires >=3.12) or PEP 668 externally-managed,
# and the unit runs with ProtectHome=true (a venv in a user's $HOME would be
# hidden from the service). The unit runs <install-dir>/venv/bin/python;
# override EGRESS_AGENT_VENV via a systemd drop-in if a host installs elsewhere.
#
# It never writes credentials and never starts the service against a config
# that does not exist yet.
set -euo pipefail

INSTANCE="${1:?usage: install.sh <instance> [--user <user>] [--port <port>] [--python <interp>] [--install-dir <dir>] [--pip-index <url>]}"
shift || true
RUN_USER=""
PORT=""
PYTHON=""
INSTALL_DIR="/opt/egress-agent"
PIP_INDEX=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --pip-index) PIP_INDEX="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT_DIR="$REPO_ROOT/egress-agent"

# Pick a Python >= 3.12 for the venv unless one was given explicitly.
if [[ -z "$PYTHON" ]]; then
  for cand in python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 12) else 1)' 2>/dev/null; then
      PYTHON="$cand"
      break
    fi
  done
fi
if [[ -z "$PYTHON" ]]; then
  echo "no Python >= 3.12 found; pass --python /path/to/python3.12" >&2
  exit 1
fi

echo "==> create venv at $INSTALL_DIR/venv (python: $PYTHON)"
install -d -m 755 "$INSTALL_DIR"
"$PYTHON" -m venv "$INSTALL_DIR/venv"

PIP_ARGS=(install --quiet --upgrade)
[[ -n "$PIP_INDEX" ]] && PIP_ARGS+=(--index-url "$PIP_INDEX")
echo "==> pip install egress-agent"
"$INSTALL_DIR/venv/bin/pip" "${PIP_ARGS[@]}" "$AGENT_DIR"

echo "==> install systemd unit"
install -Dm644 "$AGENT_DIR/deploy/egress-agent@.service" \
  "/etc/systemd/system/egress-agent@.service"
systemctl daemon-reload

echo "==> prepare config/log dirs"
install -d -m 750 /etc/egress-agent
install -d -m 755 /var/log/egress-agent
if [[ -n "$RUN_USER" ]]; then
  # Owner only: the run user may have no same-named group (e.g. owin10-bucket-b
  # is gid 1000). systemd then uses the user's primary group.
  chown -R "$RUN_USER" /etc/egress-agent /var/log/egress-agent
fi

if [[ -n "$PORT" ]]; then
  echo "==> NOTE: set listener.port=$PORT in /etc/egress-agent/$INSTANCE.json"
fi

echo
echo "next (manual, secrets never automated):"
echo "  1. cp $AGENT_DIR/deploy/config.example.json /etc/egress-agent/$INSTANCE.json"
echo "  2. edit it (egress.name, bind_source, listener auth, onts[].password), chmod 600"
echo "  3. egress-agent status --config /etc/egress-agent/$INSTANCE.json"
if [[ -n "$RUN_USER" ]]; then
  echo "  4. sudo systemctl edit egress-agent@$INSTANCE  # User=$RUN_USER / Group=$RUN_USER"
  echo "  5. sudo systemctl enable --now egress-agent@$INSTANCE"
else
  echo "  4. sudo systemctl enable --now egress-agent@$INSTANCE"
fi
