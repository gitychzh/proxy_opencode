"""proxy_opencode: OpenAI-compatible gateway in front of local `opencode serve`.

Architecture overview (see docs/architecture.md):

    client (hermes, any OpenAI SDK)
        -> POST /v1/chat/completions  (this gateway, FastAPI)
        -> UpstreamAdapter            (protocol, one impl per upstream mode)
        -> opencode-serve adapter     (drives official local `opencode serve`)
        -> official opencode CLI      (talks to Zen, incl. free-tier rules)

The gateway never spoofs the opencode client fingerprint and never proxies
Zen directly: free-tier enforcement belongs to the official server.
"""

__version__ = "0.2.0"
