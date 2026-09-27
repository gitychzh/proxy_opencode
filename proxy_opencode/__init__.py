"""proxy_opencode: OpenAI-compatible gateway in front of OpenCode Zen.

Architecture overview:

    client (hermes, any OpenAI SDK)
        -> POST /v1/chat/completions  (this gateway, FastAPI)
        -> UpstreamAdapter            (protocol, one impl per upstream mode)
           - zen-direct (default): direct OpenAI-compatible calls to
             OpenCode Zen with the reconstructed opencode client protocol
             (free-tier gate: system-prompt marker + builtin tools + stream)
           - opencode-serve: drives local official `opencode serve`
           - openai: plain passthrough to any OpenAI-compatible endpoint

Observability: every chat completion logs request_id/model/stream/has_tools/
status/latency_ms/usage/client — but never message content or any key.
LOG_FORMAT=json switches logs to JSON lines (see logsetup.py).
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version

try:
    __version__ = _package_version("proxy_opencode")
except PackageNotFoundError:  # not installed (e.g. running from a checkout)
    __version__ = "0.0.0+unknown"
