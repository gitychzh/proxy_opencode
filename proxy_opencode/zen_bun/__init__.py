"""bun transport assets for the zen-direct adapter.

Only ``forward.ts`` lives here (the bundled upstream-transport script the
gateway drives per attempt). See ``proxy_opencode.upstreams.zen_bun_transport``
for the python side and the incident note explaining why a bun/boringssl/h2
hop is required.
"""
