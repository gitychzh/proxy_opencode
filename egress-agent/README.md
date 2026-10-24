# egress-agent

Device-side egress proxy for the 0.7.x `proxy_opencode` gateway stack
(architecture: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)).

## What it is

One small daemon per **uplink** (one quota bucket):

* listens on a single port (`10001`, or `10002` for a second uplink on the
  same device) and serves HTTP `CONNECT` to the gateway's egress pool —
  every tunneled connection leaves through this device's own public IP,
  which is what the Zen free tier meters;
* serves `GET /healthz` with the currently observed egress IP so the
  gateway pool can verify buckets and detect IP changes;
* can reboot the device's ONT/router to renew the public IP
  (`rotate`, Huawei HTTP driver) — wired to the gateway's 500-request
  counter in P5.

It is **not** the gateway: no prompts, no keys, no model logic, no payload
inspection — and by design it never logs tunnel targets or content.

## Platform builds

| Platform | Status |
|---|---|
| Ubuntu / Linux (incl. Android chroots) | **P1 target** (this package, Python) |
| Windows | 已落地（owin10 双桶 2026-10-25，`deploy/windows/README.md`：schtasks SYSTEM 自启 + `bind_source` 钉源） |
| Android (native, Kotlin) | placeholder, `../egress-agent-android/` |

## Config

JSON (stdlib-only), mode `600` on the device, real credentials never in the
repo. See [`deploy/config.example.json`](deploy/config.example.json) for a
fully commented example and the opc2 dual-uplink pattern.

```bash
egress-agent status --config /etc/egress-agent/agent.json        # secret-free
egress-agent run --check --config /etc/egress-agent/agent.json   # validate only
```

## Runtime (one port, three duties)

| Endpoint | Auth | Purpose |
|---|---|---|
| `CONNECT host:port` | Proxy-Authorization (if configured) | The gateway's egress tunnels |
| `GET /healthz` | none (no secrets in payload) | egress name + observed egress IP + counters |
| `POST /rotate` | Authorization **or** Proxy-Authorization | reboot the configured ONT |

```bash
# gateway-facing smoke test (Authorization, like the pool calls it)
curl -sS -X POST -u user:pass -H 'Content-Type: application/json' \
  -d '{"ont":"v173-50","wait":false}' http://<device>:10001/rotate
```

Device install notes (PyPI slow on CN networks → prefer a local mirror;
the opc2 dual-uplink uid-routing gotchas): [`deploy/README.md`](deploy/README.md).

## Local development

```bash
cd egress-agent
pip install -e '.[test,dev]'
pytest tests/ -q
ruff check egress_agent tests
mypy egress_agent
```

These three gates run in CI for every push (`.github/workflows/ci.yml`,
job `egress-agent`), next to the gateway's gates — the same "all green or
no commit" rule.
