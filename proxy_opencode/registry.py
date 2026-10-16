"""Public model registry: the alias layer between users and upstreams.

Users only ever see the public catalogue (e.g. the deepseek alias). Every
request's model field is resolved to the upstream id, and every response's
model field is rewritten back to the public id — so the upstream model name
never leaks through the *masked* paths.

Exception — free-model passthrough (P3): the full free-model list is served
only by the hidden ``/v1/models1`` endpoint, and a client that deliberately
names a free upstream id (learned there) gets it passed through verbatim,
including the echoed ``model`` field. Masking still applies to everything
else, so scanning agents on the standard paths can never enumerate upstream
ids.
"""

from __future__ import annotations

from typing import Any, Iterable

from .config import Settings


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self._entries = list(settings.public_models)
        self.enabled = bool(settings.mask_models) and bool(self._entries)
        if not self._entries:
            # Unmasked passthrough mode: resolve/public_id become identity.
            self.enabled = False
        self._free_ids: frozenset[str] = frozenset()

    # ------------------------------------------------------ free passthrough

    def set_free_ids(self, ids: Iterable[str]) -> None:
        """Seed the free-model passthrough set (called by the catalog).

        Membership makes ``resolve``/``public_id`` echo the id verbatim:
        the client already knows the raw upstream name (the catalog gave it
        to them), so rewriting would only break ``model`` round-tripping.
        """

        cleaned = frozenset(
            i for i in (str(x).strip() for x in ids) if i
        )
        self._free_ids = cleaned

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

        Public alias entries win (masking), then the free-model passthrough
        set (verbatim ids served via /v1/models1), and unknown ids fall
        back to the default public model's upstream: the gateway always
        serves something instead of 404-ing on a stale catalog.
        """
        if not self.enabled:
            return str(requested or "")
        raw = str(requested or "").strip()
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["upstream"]
        if raw in self._free_ids:
            return raw
        return self.default_upstream

    def public_id(self, model: Any) -> str:
        """Any upstream/echoed model id -> the public id shown to users.

        Same order as `resolve`: masked entries rewrite upstream names to
        the alias; deliberately-requested free ids echo unchanged.
        """
        if not self.enabled:
            return str(model or "")
        raw = str(model or "").strip()
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["id"]
        if raw in self._free_ids:
            return raw
        return self.default_public_id
