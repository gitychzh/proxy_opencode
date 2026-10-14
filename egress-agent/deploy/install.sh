#!/usr/bin/env bash
# Install egress-agent on an Ubuntu/device host (idempotent).
#
# Usage:
#   sudo ./install.sh <instance> [--user <user>] [--port <port>]
#
# What it does (nothing else, nothing destructive):
#   1. pip-install the agent + deps into the system python (or --user scope)
#   2. install the systemd template unit
#   3. ensure /etc/egress-agent + /var/log/egress-agent exist (600 on config)
#   4. print the next manual step: drop the real config and enable the unit
#
# It never writes credentials and never starts the service against a config
# that does not exist yet.
set -euo pipefail

INSTANCE="${1:?usage: install.sh <instance> [--user <user>] [--port <port>]}"
shift || true
RUN_USER=""
PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) RUN_USER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AGENT_DIR="$REPO_ROOT/egress-agent"

echo "==> pip install egress-agent (system python)"
python3 -m pip install --quiet --upgrade "$AGENT_DIR"

echo "==> install systemd unit"
install -Dm644 "$AGENT_DIR/deploy/egress-agent@.service" \
  "/etc/systemd/system/egress-agent@.service"
systemctl daemon-reload

echo "==> prepare config/log dirs"
install -d -m 750 /etc/egress-agent
install -d -m 755 /var/log/egress-agent
if [[ -n "$RUN_USER" ]]; then
  chown -R "$RUN_USER":"$RUN_USER" /etc/egress-agent /var/log/egress-agent
fi

if [[ -n "$PORT" ]]; then
  echo "==> NOTE: set listener.port=$PORT in /etc/egress-agent/$INSTANCE.json"
fi

echo
echo "next (manual, secrets never automated):"
echo "  1. cp $AGENT_DIR/deploy/config.example.json /etc/egress-agent/$INSTANCE.json"
echo "  2. edit it (egress.name, bind_source, onts[].password), chmod 600"
echo "  3. egress-agent status --config /etc/egress-agent/$INSTANCE.json"
if [[ -n "$RUN_USER" ]]; then
  echo "  4. sudo systemctl edit egress-agent@$INSTANCE  # User=$RUN_USER"
  echo "  5. sudo systemctl enable --now egress-agent@$INSTANCE"
else
  echo "  4. sudo systemctl enable --now egress-agent@$INSTANCE"
fi
