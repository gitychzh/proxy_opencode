"""OpenAI-style error responses."""

from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error(
    status: int,
    message: str,
    err_type: str = "api_error",
    code: str | None = None,
) -> JSONResponse:
    """Build an OpenAI api-error shaped JSON response."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "param": None,
                "code": code,
            }
        },
    )


class GatewayHttpError(Exception):
    """Exception carrying a ready-made OpenAI-style JSON response."""

    def __init__(self, response: JSONResponse) -> None:
        super().__init__(response.status_code)
        self.response = response


class InvalidRequest(ValueError):
    """A client-fault request shape error, safe to answer as HTTP 400.

    The pipeline used to map EVERY ``ValueError`` out of an adapter onto
    "invalid_request_error" — including accidental ones raised while parsing
    an upstream body. Production evidence (2026-10-25, 2.2s, usage=null, one
    occurrence in 24h): the client was told its request was malformed when
    the upstream answer had actually tripped an ``int()``/parse path. A fake
    400 also suppresses the failover ladder (request-level errors are never
    retried on another bucket), so the misattribution costs availability,
    not just clarity.

    Raise this — and only this — when the request itself is the problem.
    Anything else a ``ValueError``-derived exception could mean must surface
    as a 5xx.
    """


class ContextOverflow(InvalidRequest):
    """The prompt is larger than the model's advertised input limit.

    Deliberately the same verdict a real provider returns, because opencode
    treats a "context too long" rejection as a compaction trigger
    (``session/compaction.ts``: ``{reason: "overflow"}``). Answering with a
    gateway-only shape would make the CLI retry the identical oversized
    prompt. Carries the numbers so the reply can be acted on instead of
    guessed at.
    """

    def __init__(
        self,
        *,
        model: str,
        estimated_tokens: int,
        input_limit: int,
        context_limit: int,
        compaction_ceiling: int,
        prompt_chars: int = 0,
        gate_chars: int = 0,
        tool_chars: int = 0,
    ) -> None:
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.input_limit = input_limit
        self.context_limit = context_limit
        self.compaction_ceiling = compaction_ceiling
        # Populated only when the free tier's character gate is what fired.
        self.prompt_chars = prompt_chars
        self.gate_chars = gate_chars
        self.tool_chars = tool_chars
        super().__init__(self.describe())

    def describe(self) -> str:
        """Client-facing sentence naming the limit and the compaction point.

        The leading sentence is byte-identical to the vendor's own 400 so a
        client that already parses it needs no new branch. The trailing
        ``[proxy_opencode]`` clause exists because the vendor's sentence is
        actively misleading on the free tier: it advertises the *model* window
        (200000 tokens, "compacts below 170000") while the account can never
        send more than 32000 prompt tokens. A client that budgets against the
        advertised ceiling loops forever — 2026-11-18, dsh compacted 16 times
        and never recovered.
        """

        ceiling = self.compaction_ceiling or self.input_limit
        if self.gate_chars > 0:
            # The reachable compaction point is the gate, not the window.
            ceiling = min(ceiling, self.input_limit)
        base = (
            f"This model's maximum context length is {self.context_limit} tokens "
            f"and at most {self.input_limit} prompt tokens are accepted. This "
            f"request is about {self.estimated_tokens} prompt tokens. Reduce the "
            f"prompt or compact the conversation (the client compacts below "
            f"{ceiling} tokens) and retry."
        )
        if self.gate_chars <= 0:
            return base
        message_chars = max(0, self.prompt_chars - self.tool_chars)
        return (
            f"{base} [proxy_opencode] The free tier enforces a {self.gate_chars}"
            f"-character prompt cap, not the advertised token window: this "
            f"request carried {self.prompt_chars} characters ({self.tool_chars} "
            f"in tool definitions, {message_chars} in messages). Trim at least "
            f"{max(0, self.prompt_chars - self.gate_chars)} characters — tool "
            f"definitions count toward the cap."
        )
