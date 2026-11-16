"""Public model registry: pure passthrough by default.

2026-11-17 (owner): the historical alias layer is gone. ``PUBLIC_MODELS`` is
empty by default, so ``resolve``/``public_id`` are identity and every model id
reaches the upstream verbatim. There is no paid/free distinction at this
gateway — the free-model classification only decides what the *upstream*
accepts, never what this gateway forwards.

A deployment that explicitly sets ``PUBLIC_MODELS`` still gets the legacy
behaviour: aliases (``id:display:upstream``) plus verbatim free snapshot ids,
with everything else rejected (404). That path is kept for operators who want
to narrow the surface; it is not the default and not what the owner runs.
"""

from __future__ import annotations

from typing import Any

from .config import Settings
from .egress.free_models import FreeModelsSnapshot


class UnknownModelError(Exception):
    """Client-supplied model id outside the public alias set.

    Owner requirement (2026-10-16): the model id must be exactly right —
    unknown ids are rejected (404) instead of silently resolving to the
    default upstream. Response-side masking (`public_id`) is NOT strict:
    unknown upstream echoes must still be masked, not failed.
    """

    def __init__(self, requested: Any) -> None:
        self.requested = requested
        super().__init__(str(requested or "<missing model>"))


class ModelRegistry:
    def __init__(
        self, settings: Settings, snapshot: FreeModelsSnapshot | None = None
    ) -> None:
        self._entries = list(settings.public_models)
        self._snapshot = snapshot
        self.enabled = bool(settings.mask_models) and bool(self._entries)
        if not self._entries:
            # Unmasked passthrough mode: resolve/public_id become identity.
            self.enabled = False

    # ---------------------------------------------------------------- read

    @property
    def default_upstream(self) -> str:
        """Default upstream for an empty/absent model id.

        2026-11-17: the alias layer is gone, so there may be no configured
        entry. Fall back to the first relay model instead of indexing an
        empty list.
        """

        if self._entries:
            return str(self._entries[0]["upstream"])
        return ""

    @property
    def default_public_id(self) -> str:
        if self._entries:
            return str(self._entries[0]["id"])
        return ""

    def _free_ids(self) -> list[str]:
        return self._snapshot.ids() if self._snapshot is not None else []

    def catalog(self) -> dict[str, Any]:
        """OpenAI-shaped GET /v1/models body listing public ids.

        Strictly standard fields (id/object/created/owned_by) — clients such
        as hermes auto-discover models from this body and must not need to
        understand gateway extras. Aliases come first, then free bare ids.
        """

        ids = [e["id"] for e in self._entries]
        for mid in self._free_ids():
            if mid not in ids:
                ids.append(mid)
        return {
            "object": "list",
            "data": [
                {
                    "id": mid,
                    "object": "model",
                    "created": 0,
                    "owned_by": "gateway",
                }
                for mid in ids
            ],
        }

    # -------------------------------------------------------------- lookup

    def resolve(self, requested: Any) -> str:
        """Any client-requested model id -> the upstream model to call.

        2026-11-17 (owner): there is no alias layer any more. With the default
        empty catalogue this is pure passthrough — every id reaches the
        upstream verbatim, which is what the owner requires ("完全透传ID").
        A deployment that still configures ``PUBLIC_MODELS`` keeps the legacy
        alias/whitelist behaviour: aliases resolve to their upstream, snapshot
        ids pass verbatim, everything else raises `UnknownModelError`.
        """

        if not self.enabled:
            return str(requested or "")
        raw = str(requested or "").strip()
        for e in self._entries:
            if raw == e["id"]:
                return e["upstream"]
        if raw in self._free_ids():
            return raw
        raise UnknownModelError(raw)

    def public_id(self, model: Any) -> str:
        """Any upstream/echoed model id -> the id shown back to the client.

        Passthrough mode returns the id unchanged, so the answer is always
        labelled with the model the caller actually asked for. With a
        configured catalogue, aliases and their upstream spellings map back to
        the alias and anything unrecognised is masked.
        """

        if not self.enabled:
            return str(model or "")
        raw = str(model or "").strip()
        if raw in self._free_ids():
            return raw
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["id"]
        return self.default_public_id
