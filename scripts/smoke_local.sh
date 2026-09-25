#!/usr/bin/env bash
# Local end-to-end smoke test for proxy_opencode.
#
# Starts the fake upstream and the real gateway on random localhost ports
# (no real upstream key needed), then verifies healthz / models / chat
# (non-stream + SSE stream) / tools+reasoning passthrough / 401 / 429.
# All keys are dummy values; nothing sensitive is printed.

set -euo pipefail
cd "$(dirname "$0")/.."

GATEWAY_KEY="gw-dummy-key"
UPSTREAM_KEY="upstream-dummy-key"
MODEL="fake-openai-model"

free_port() {
  python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

UPSTREAM_PORT="$(free_port)"
GATEWAY_PORT="$(free_port)"
UPSTREAM_URL="http://127.0.0.1:${UPSTREAM_PORT}"
GATEWAY_URL="http://127.0.0.1:${GATEWAY_PORT}"

UPSTREAM_PID=""
GATEWAY_PID=""
cleanup() {
  [ -n "${GATEWAY_PID}" ] && kill "${GATEWAY_PID}" 2>/dev/null || true
  [ -n "${UPSTREAM_PID}" ] && kill "${UPSTREAM_PID}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

wait_up() {
  local url="$1" name="$2" i
  for i in $(seq 1 100); do
    if curl -sf -o /dev/null "$url"; then return 0; fi
    sleep 0.1
  done
  echo "FAIL: $name did not start" >&2
  exit 1
}

PASS=0
ok() { PASS=$((PASS + 1)); echo "ok ${PASS} - $1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

# --- start servers ---
python tests/fake_upstream.py --port "${UPSTREAM_PORT}" &
UPSTREAM_PID=$!

UPSTREAM_BASE_URL="${UPSTREAM_URL}" \
UPSTREAM_API_KEY="${UPSTREAM_KEY}" \
GATEWAY_API_KEYS="${GATEWAY_KEY},gw-rl-key" \
REQUESTS_PER_MINUTE=8 \
REASONING_PASSTHROUGH=true \
python -m uvicorn proxy_opencode.app:app \
  --host 127.0.0.1 --port "${GATEWAY_PORT}" --log-level warning &
GATEWAY_PID=$!

wait_up "${UPSTREAM_URL}/v1/models" "fake upstream"
wait_up "${GATEWAY_URL}/healthz" "gateway"

# --- 1. healthz ---
curl -sf "${GATEWAY_URL}/healthz" | grep -q '"status":"ok"' \
  && ok "healthz" || fail "healthz"

# --- 2. models forwarding (upstream key swapped in) ---
models="$(curl -sf "${GATEWAY_URL}/v1/models" -H "Authorization: Bearer ${GATEWAY_KEY}")"
echo "${models}" | grep -q "\"${MODEL}\"" && ok "models forwarded" \
  || fail "models forwarded"
EXPECTED_FP="$(printf '%s' "${UPSTREAM_KEY}" | python -c \
  'import sys,hashlib; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12])')"
echo "${models}" | grep -q "${EXPECTED_FP}" && ok "upstream sees UPSTREAM_API_KEY, not gateway key" \
  || fail "key swap check"

# --- 3. non-stream chat ---
chat="$(curl -sf "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${GATEWAY_KEY}" -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}")"
echo "${chat}" | grep -q "fake upstream reply" && ok "chat non-stream" \
  || fail "chat non-stream"

# --- 4. SSE stream ---
stream="$(curl -sfN "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${GATEWAY_KEY}" -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}")"
echo "${stream}" | grep -q "data: \[DONE\]" && echo "${stream}" | grep -q '"content": *"fake "' \
  && ok "chat SSE stream" || fail "chat SSE stream"

# --- 5. tools + reasoning passthrough ---
tr="$(curl -sf "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${GATEWAY_KEY}" -H "Content-Type: application/json" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"weather?\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\"}}],\"tool_choice\":\"auto\",\"reasoning_effort\":\"high\"}")"
echo "${tr}" | grep -q "get_weather" && echo "${tr}" | grep -q '"reasoning_effort":"high"' \
  && ok "tools + reasoning passthrough" || fail "tools + reasoning passthrough"

# --- 6. 401 without / with wrong key ---
code="$(curl -s -o /dev/null -w '%{http_code}' "${GATEWAY_URL}/v1/models")"
[ "${code}" = "401" ] && ok "401 without key" || fail "401 without key (got ${code})"
code="$(curl -s -o /dev/null -w '%{http_code}' "${GATEWAY_URL}/v1/models" \
  -H "Authorization: Bearer wrong")"
[ "${code}" = "401" ] && ok "401 with wrong key" || fail "401 with wrong key (got ${code})"

# --- 7. 429 rate limit (separate key with per-minute limit 8) ---
code=""
for i in $(seq 1 9); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${GATEWAY_URL}/v1/models" \
    -H "Authorization: Bearer gw-rl-key")"
done
[ "${code}" = "429" ] && ok "429 rate limit" || fail "429 rate limit (got ${code})"

echo
echo "All ${PASS} smoke checks passed."
