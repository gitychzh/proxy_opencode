"""Public model registry: aliases plus verbatim free-model passthrough.

Users see the public catalogue. By default that is exactly one alias,
``deepseek-ai/deepseek-v4.1-flash``. When ``ZEN_FREE_MODELS_FILE`` points at
the daily snapshot written by ``scripts/sync_zen_free_models.py``, every free
bare id in that snapshot is published verbatim too and resolves to itself (no
rewrite), so a client can address any opencode free model by its real id.

Strict contract (owner, 2026-10-17): with no snapshot configured the alias is
still the ONLY accepted model id. With a snapshot the accepted set is the
alias plus exactly the ids in the snapshot; everything else is a 404.

Response-side masking stays non-strict: unknown upstream echoes are still
masked to the default alias, while a passthrough id is echoed back unchanged
so two models can never be confused inside one client conversation.
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
        return self._entries[0]["upstream"]

    @property
    def default_public_id(self) -> str:
        return self._entries[0]["id"]

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

        Strict: aliases resolve to their configured upstream; ids from the
        daily free snapshot are passed through verbatim; anything else (and
        the raw upstream id of an alias) raises `UnknownModelError`.
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
        """Any upstream/echoed model id -> the public id shown to users.

        A passthrough id is returned unchanged so an answer to model A is
        never labelled as model B. Aliases and their upstream spellings map
        back to the alias; anything unrecognised still gets masked.
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
