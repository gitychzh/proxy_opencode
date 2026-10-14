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
| Windows | placeholder, P6 (same package, `deploy/windows/`) |
| Android (native, Kotlin) | placeholder, `../egress-agent-android/` |

## Config

JSON (stdlib-only), mode `600` on the device, real credentials never in the
repo. See [`deploy/config.example.json`](deploy/config.example.json) for a
fully commented example and the opc2 dual-uplink pattern.

```bash
egress-agent status --config /etc/egress-agent/agent.json        # secret-free
egress-agent run --check --config /etc/egress-agent/agent.json   # validate only
```

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
