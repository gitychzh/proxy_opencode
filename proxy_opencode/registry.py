"""Public model registry: the alias layer between users and upstreams.

Users only ever see the public catalogue (e.g. `ds41f_cus`). Every request's
model field is resolved to the upstream id, and every response's model field
is rewritten back to the public id — so the upstream model name never leaks,
in JSON bodies or streamed SSE chunks alike.
"""

from __future__ import annotations

from typing import Any

from .config import Settings


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self._entries = list(settings.public_models)
        self.enabled = bool(settings.mask_models) and bool(self._entries)
        if not self._entries:
            # Unmasked passthrough mode: resolve/public_id become identity.
            self.enabled = False

    # ---------------------------------------------------------------- read

    @property
    def default_upstream(self) -> str:
        return self._entries[0]["upstream"]

    @property
    def default_public_id(self) -> str:
        return self._entries[0]["id"]

    def catalog(self) -> dict[str, Any]:
        """OpenAI-shaped GET /v1/models body listing only public models."""
        return {
            "object": "list",
            "data": [
                {
                    "id": e["id"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "gateway",
                    "display_name": e["display_name"],
                }
                for e in self._entries
            ],
        }

    # -------------------------------------------------------------- lookup

    def resolve(self, requested: Any) -> str:
        """Any client-requested model id -> the upstream model to call.

        Unknown ids (including direct attempts to name an upstream model)
        transparently resolve to the default public model's upstream: the
        gateway serves exactly one model regardless of what clients send.
        """
        if not self.enabled:
            return str(requested or "")
        raw = str(requested or "").strip()
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["upstream"]
        return self.default_upstream

    def public_id(self, model: Any) -> str:
        """Any upstream/echoed model id -> the public id shown to users."""
        if not self.enabled:
            return str(model or "")
        raw = str(model or "").strip()
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["id"]
        return self.default_public_id


def build_registry(settings: Settings) -> ModelRegistry:
    return ModelRegistry(settings)
