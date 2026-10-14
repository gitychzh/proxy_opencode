# egress-agent — Android (Kotlin) build

**Status: placeholder (P6).** The Ubuntu/Python agent (see
[`../egress-agent`](../egress-agent)) covers all current bucket devices —
including the Android phones, which run their agents inside an Ubuntu
chroot. The native Kotlin build exists in the plan for the day a bucket
runs the agent directly on Android (no chroot).

## Planned shape

| Aspect | Plan |
|---|---|
| Language/runtime | Kotlin, single static APK-less binary via `native-image` or a tiny JVM service |
| Proxy core | `io.ktor`/raw `ServerSocket` CONNECT relay + coroutine duplex pump |
| Egress binding | `Socket.bind` to the uplink address; `ConnectivityManager` per-network binding where needed |
| Health | `GET /healthz` on the agent port (same contract as the Python agent — the gateway consumes one wire format) |
| Rotation | Same Huawei four-step HTTP flow (OkHttp), driven by the gateway via the control endpoint |
| Deployment | `su -c` start script + boot receiver; config JSON in app-private storage (mode 600) |

## Wire contract freeze (so both builds stay interchangeable)

`GET /healthz` → `{"egress": "<name>", "ready": true, "egress_ip": "<ip>",
"egress_ip_at": "<iso8601>", "tunnels_active": 0, "tunnels_total": 0}`

`POST /rotate` `{"ont": "<name>"}` → `{"ok": true, "previous_ip": "...",
"new_ip": "...", "elapsed_s": 95}` — the gateway's pool only ever talks
this contract, never to a driver directly.

Any change to this contract must update **both** builds and the gateway
pool's parser together (single CI gate per build).
