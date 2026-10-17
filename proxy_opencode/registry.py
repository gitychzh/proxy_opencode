"""Public model registry: the alias layer between users and upstreams.

Users only ever see the public catalogue — by default exactly one alias,
``deepseek-ai/deepseek-v4.1-flash``. Every request's model field is resolved
to the upstream id and every response's model field is rewritten back to the
public id, so the upstream model name (``big-pickle``) never leaks through
*any* client-visible path (JSON body, SSE chunks, /v1/models).

Strict contract (owner, 2026-10-17): the public alias is the ONLY accepted
model id. The raw upstream spelling is rejected (404) just like any other
unknown id — a client cannot address the backend id even if it guesses it —
and nothing is relayed beyond the ``PUBLIC_MODELS`` entries. The standard
``/v1/models`` body is plain OpenAI shape (id/object/created/owned_by) so
discovery agents such as hermes can consume it unmodified.
"""

from __future__ import annotations

from typing import Any

from .config import Settings


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
        """OpenAI-shaped GET /v1/models body listing only public models.

        Strictly standard fields (id/object/created/owned_by) — clients such
        as hermes auto-discover models from this body and must not need to
        understand gateway extras.
        """

        return {
            "object": "list",
            "data": [
                {
                    "id": e["id"],
                    "object": "model",
                    "created": 0,
                    "owned_by": "gateway",
                }
                for e in self._entries
            ],
        }

    # -------------------------------------------------------------- lookup

    def resolve(self, requested: Any) -> str:
        """Any client-requested model id -> the upstream model to call.

        STRICT (owner requirement): only the public alias ids are accepted —
        the raw upstream spelling (``big-pickle``) is just as unknown as a
        typo'd id and raises `UnknownModelError`, so the backend model name
        is unaddressable from the client side.
        """
        if not self.enabled:
            return str(requested or "")
        raw = str(requested or "").strip()
        for e in self._entries:
            if raw == e["id"]:
                return e["upstream"]
        raise UnknownModelError(raw)

    def public_id(self, model: Any) -> str:
        """Any upstream/echoed model id -> the public id shown to users.

        Every upstream spelling (bare id, legacy ``provider/model`` echo,
        anything unrecognised) maps back to the alias; there is no
        passthrough path that could surface a backend id.
        """
        if not self.enabled:
            return str(model or "")
        raw = str(model or "").strip()
        for e in self._entries:
            if raw in (e["id"], e["upstream"]):
                return e["id"]
        return self.default_public_id
