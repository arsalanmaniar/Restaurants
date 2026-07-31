"""Instrumentation tables — measurement only, never customer-facing."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# The model's rejected output is kept only as a HEAD. Long enough to carry the
# tool name (which is always at the start) and the beginning of the arguments,
# short enough that this measurement table is not a second full copy of customer
# text. `generation_length` records what the original was, so a class of failure
# whose break is at the END — unterminated JSON on a long place_order call —
# still shows up as "we truncated a lot" rather than silently disappearing.
MAX_GENERATION_CHARS = 200


class ToolCallFailure(Base):
    """One row per tool call Groq rejected as malformed (`tool_use_failed`).

    Exists because this failure was only observable in Render logs, which made
    "how often does this happen, and for which tool?" unanswerable from here. It
    reached customers: conv 724 produced two FALLBACK_REPLY turns in four
    messages, and the only thing distinguishing "malformed three times" from
    "model replied without calling a tool" was a log line.

    Rows are written from their OWN session, committed immediately — see
    services/diagnostics.py for why that matters.
    """

    __tablename__ = "tool_call_failures"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Groups the attempts within a single generate_reply, so the retry-recovery
    # ratio is computable: a turn_id with no gave_up row recovered on retry.
    turn_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # A plain indexed integer, deliberately NOT a foreign key.
    #
    # A measurement table must not take locks on an operational one. With an FK,
    # inserting a row whose parent conversation was created by a still-open
    # transaction makes Postgres BLOCK — it waits to see whether that transaction
    # commits. This recorder runs mid-turn, so it would be waiting on the very
    # transaction it is instrumenting. It showed up immediately as a 6x slowdown
    # of the test suite (1:15 -> 7:14), where every test holds an uncommitted
    # transaction by design.
    #
    # Referential integrity buys nothing here: nothing joins this table for
    # correctness, and the row is worth keeping even if the conversation is later
    # purged — which the previous ON DELETE SET NULL was already conceding.
    conversation_id: Mapped[int | None] = mapped_column(Integer, index=True)

    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # True when this attempt ended the turn — either retries ran out, or tools had
    # already run so no retry was attempted at all.
    gave_up: Mapped[bool] = mapped_column(nullable=False, default=False)

    # Best-effort parse of the rejected generation. Null when unparseable, which
    # is itself a finding worth counting.
    tool_name: Mapped[str | None] = mapped_column(String(64), index=True)

    failed_generation: Mapped[str | None] = mapped_column(Text)
    generation_length: Mapped[int | None] = mapped_column(Integer)

    # Shape of the error payload, not its contents: the code, plus the KEY NAMES
    # present in the body. Keys answer "is failed_generation even the right field
    # name?" — the question that motivated capturing the raw body — while carrying
    # no customer text at all.
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_keys: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
