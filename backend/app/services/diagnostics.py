"""Recording failures we could otherwise only see in logs.

Two properties this module must have, both of which shape the implementation:

1. **The record must survive the turn being rolled back.** The failure it records
   happens inside `generate_reply`, inside the `try` in `handle_incoming_message`
   whose `except` calls `db.rollback()`. Writing through the turn's session would
   discard the record exactly when we most need it. Committing the turn's session
   instead is worse — it would commit partial cart or order state mid-flight. So
   each record is written from its OWN short-lived session and committed
   immediately.

2. **It must never break a customer's turn.** Every entry point swallows its own
   exceptions. A diagnostics table that takes down ordering would be a strictly
   worse bug than the one it measures.
"""

import json
import logging
import re

from app.core.database import SessionLocal
from app.models import ToolCallFailure
from app.models.diagnostics import MAX_GENERATION_CHARS

logger = logging.getLogger(__name__)

# What llama emits when it botches a tool call. Both shapes seen in the wild:
#   <function=find_restaurants{"query": "zinger roll"}></function>
#   {"name": "find_restaurants", "arguments": {...}}
_TOOL_NAME_PATTERNS = (
    re.compile(r"<function=([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
    re.compile(r'"function"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"'),
)


def tool_name_from(generation: str | None) -> str | None:
    """The tool the model was reaching for, or None if it cannot be read.

    None is a finding, not a gap — a generation with no recognisable tool name is
    a different failure from a malformed argument list, and worth counting apart.
    """
    if not generation:
        return None
    for pattern in _TOOL_NAME_PATTERNS:
        match = pattern.search(generation)
        if match:
            return match.group(1)[:64]
    return None


def _unpack(exc: Exception) -> tuple[str | None, str | None, str | None]:
    """(failed_generation, error_code, error_keys) read defensively.

    The Groq SDK has no typed field for `failed_generation` — the name comes from
    the API's observed behaviour, not from anything we can rely on. So the body is
    read by duck-typing, and its KEY NAMES are recorded separately: if the field
    is ever named something else, the keys show us what to read instead, while
    carrying no customer text of their own.
    """
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        # Shape we did not expect. Keep the message so the row is still useful.
        return str(exc)[:MAX_GENERATION_CHARS], None, None

    generation = error.get("failed_generation")
    if generation is not None and not isinstance(generation, str):
        generation = json.dumps(generation)
    if generation is None:
        # Field missing or renamed — fall back to the message text so a row is
        # still recorded, and let error_keys reveal the real field name.
        generation = str(getattr(exc, "message", "") or str(exc))

    code = error.get("code")
    return generation, (str(code)[:64] if code else None), ",".join(sorted(error))[:200]


def record_tool_call_failure(
    *, turn_id: str, conversation_id: int | None, attempt: int, gave_up: bool, exc: Exception
) -> None:
    """Record one rejected tool call. Never raises."""
    try:
        generation, code, keys = _unpack(exc)
        session = SessionLocal()
        try:
            session.add(ToolCallFailure(
                turn_id=turn_id,
                conversation_id=conversation_id,
                attempt=attempt,
                gave_up=gave_up,
                tool_name=tool_name_from(generation),
                # Head only — see MAX_GENERATION_CHARS for the reasoning.
                failed_generation=(generation or "")[:MAX_GENERATION_CHARS] or None,
                generation_length=len(generation) if generation else None,
                error_code=code,
                error_keys=keys,
            ))
            session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 - instrumentation must never break a turn
        logger.warning("could not record tool-call failure", exc_info=True)
