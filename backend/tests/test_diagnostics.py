"""Malformed tool calls are recorded so the rate is answerable from the DB.

This failure was observable only in Render logs, which made "how often, and for
which tool?" unanswerable without dashboard access. It reaches customers — conv
724 produced two FALLBACK_REPLY turns in four messages — and nothing in the
database distinguished "malformed three times" from "model replied without
calling a tool".

The two properties that shape the implementation, and so the tests: the record
must survive the turn being rolled back, and recording must never be able to
break a turn.
"""

import types

import httpx
import pytest
from groq import BadRequestError
from sqlalchemy import select

from app.models import ToolCallFailure
from app.services import agent
from app.services import diagnostics


def _bad_request(generation='<function=find_restaurants{"query": "zinger roll"}></function>'):
    body = {"error": {
        "message": "Failed to call a function. Please adjust your prompt.",
        "type": "invalid_request_error",
        "code": "tool_use_failed",
        "failed_generation": generation,
    }}
    return BadRequestError(
        f"Error code: 400 - {body}",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.groq.test")),
        body=body,
    )


@pytest.fixture
def rows(db, monkeypatch):
    """Read the recorded rows back.

    The recorder deliberately opens its OWN session (see the module docstring), so
    under the real engine its writes land in a different transaction from the
    test's — and conftest's `db` fixture holds an uncommitted transaction for the
    whole test, so the conversation row it references is not visible there and the
    FK insert fails. The recorder swallows that, correctly, and the test would see
    nothing.

    So for these tests the recorder is pointed at a session bound to the SAME
    connection, joined by savepoint. Rows become visible and are still discarded at
    teardown. What this cannot exercise is the cross-transaction survival property
    itself — that is pinned structurally in
    TestTheRecorderDoesNotUseTheCallersSession, which is the honest way to test it
    rather than a harness that quietly proves something weaker.
    """
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(
        bind=db.get_bind(),
        join_transaction_mode="create_savepoint",
        autoflush=False,
        future=True,
    )
    monkeypatch.setattr(diagnostics, "SessionLocal", factory)

    def _read():
        db.expire_all()
        return list(db.scalars(select(ToolCallFailure).order_by(ToolCallFailure.id)).all())
    return _read


class TestToolNameParsing:
    """`tool_name` is the whole point — it is what turns "malformed calls happen"
    into "malformed calls happen on find_restaurants"."""

    def test_llama_function_tag_shape(self):
        assert diagnostics.tool_name_from(
            '<function=find_restaurants{"query": "zinger roll"}></function>'
        ) == "find_restaurants"

    def test_openai_json_shape(self):
        assert diagnostics.tool_name_from(
            '{"name": "place_order", "arguments": {"payment_method": "cod"}}'
        ) == "place_order"

    def test_unparseable_is_none_not_an_exception(self):
        """None is a finding — a generation with no recognisable tool name is a
        different failure from a malformed argument list, and counted apart."""
        assert diagnostics.tool_name_from("I'll check the menu for you") is None
        assert diagnostics.tool_name_from("") is None
        assert diagnostics.tool_name_from(None) is None


class TestRecording:
    def test_writes_a_row_with_the_diagnostic_fields(self, db, conversation, rows):
        diagnostics.record_tool_call_failure(
            turn_id="abc123", conversation_id=conversation.id,
            attempt=1, gave_up=False, exc=_bad_request(),
        )
        row = rows()[-1]
        assert row.turn_id == "abc123"
        assert row.conversation_id == conversation.id
        assert row.tool_name == "find_restaurants"
        assert row.error_code == "tool_use_failed"
        # Key NAMES, not values — this is what tells us if `failed_generation`
        # is ever renamed, without storing any customer text.
        assert "failed_generation" in row.error_keys

    def test_generation_is_truncated_but_its_length_is_kept(self, db, conversation, rows):
        """The head carries the tool name; the length preserves the signal for a
        failure whose break is at the END (unterminated JSON on a long call),
        which truncation would otherwise hide."""
        long_generation = '<function=place_order{"delivery_address": "' + "x" * 900 + '"}>'
        diagnostics.record_tool_call_failure(
            turn_id="t", conversation_id=conversation.id,
            attempt=1, gave_up=True, exc=_bad_request(long_generation),
        )
        row = rows()[-1]
        assert len(row.failed_generation) == 200
        assert row.generation_length == len(long_generation) > 200
        assert row.tool_name == "place_order", "the head must still carry the tool name"

    def test_an_unexpected_error_shape_still_records(self, db, conversation, rows):
        """Groq changing its payload must degrade to a thinner row, not no row."""
        weird = BadRequestError(
            "tool_use_failed: something new",
            response=httpx.Response(400, request=httpx.Request("POST", "https://x.test")),
            body={"unexpected": True},
        )
        diagnostics.record_tool_call_failure(
            turn_id="weird", conversation_id=conversation.id,
            attempt=1, gave_up=True, exc=weird,
        )
        assert [r for r in rows() if r.turn_id == "weird"]

    def test_a_successful_record_logs_no_warning(self, db, conversation, rows, caplog):
        """The recorder swallows its own exceptions by design, which means a
        BROKEN recorder looks exactly like a working one that found nothing. That
        is not hypothetical: the first version of these tests passed no rows and
        no warnings, because the FK insert was failing against an uncommitted
        conversation and being swallowed. Assert the quiet path is genuinely
        quiet."""
        with caplog.at_level("WARNING", logger="app.services.diagnostics"):
            diagnostics.record_tool_call_failure(
                turn_id="quiet", conversation_id=conversation.id,
                attempt=1, gave_up=False, exc=_bad_request(),
            )
        assert [r for r in rows() if r.turn_id == "quiet"], "the row must exist"
        assert "could not record" not in caplog.text, (
            f"the recorder failed silently: {caplog.text}"
        )

    def test_recording_never_breaks_the_caller(self, db, conversation, monkeypatch):
        """Instrumentation that can take down ordering would be a strictly worse
        bug than the one it measures."""
        def explode():
            raise RuntimeError("database on fire")

        monkeypatch.setattr(diagnostics, "SessionLocal", explode)
        diagnostics.record_tool_call_failure(  # must not raise
            turn_id="t", conversation_id=conversation.id,
            attempt=1, gave_up=True, exc=_bad_request(),
        )


class TestTheRecorderDoesNotUseTheCallersSession:
    """THE REASON FOR THE SEPARATE SESSION, pinned structurally.

    The failure being recorded happens inside `generate_reply`, inside the `try`
    in `handle_incoming_message` whose `except` calls `db.rollback()`. A record
    written through the turn's session would vanish exactly when it is needed;
    committing the turn's session instead would flush partial cart or order state
    mid-flight.

    The property is "opens its own session and commits it immediately". That is
    asserted here directly, rather than through a transaction-visibility test the
    harness cannot honestly run — conftest holds every test inside one rolled-back
    transaction on purpose.
    """

    def test_takes_no_session_argument_at_all(self):
        """The strongest guarantee is structural: there is no caller session to
        write through, so a future edit cannot quietly start using one."""
        import inspect

        params = inspect.signature(diagnostics.record_tool_call_failure).parameters
        assert "db" not in params and "session" not in params

    def test_opens_its_own_session_and_commits_it(self, db, conversation, monkeypatch):
        opened, committed, closed = [], [], []

        class _FakeSession:
            def add(self, obj):
                pass

            def commit(self):
                committed.append(True)

            def close(self):
                closed.append(True)

        def _factory():
            opened.append(True)
            return _FakeSession()

        monkeypatch.setattr(diagnostics, "SessionLocal", _factory)
        diagnostics.record_tool_call_failure(
            turn_id="t", conversation_id=conversation.id,
            attempt=1, gave_up=True, exc=_bad_request(),
        )

        assert opened, "must open its own session"
        assert committed, "must commit immediately, not defer to the caller"
        assert closed, "must not leak the connection"


class TestRecordedThroughTheRealLoop:
    """End to end, so the call sites are pinned and not just the recorder."""

    def _scripted_to_fail(self, monkeypatch, count):
        stream = iter([_bad_request() for _ in range(count)])

        class _Completions:
            def create(self, **kwargs):
                raise next(stream)

        monkeypatch.setattr(
            agent, "_client",
            lambda: types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=_Completions())
            ),
        )

    def test_three_malformed_calls_make_three_rows_in_one_turn(
        self, db, conversation, monkeypatch, rows,
    ):
        before = {r.id for r in rows()}
        self._scripted_to_fail(monkeypatch, 3)

        reply, trace = agent.generate_reply(db, conversation)

        assert trace == []
        assert reply == agent.FALLBACK_REPLY
        new = [r for r in rows() if r.id not in before]
        assert len(new) == 3, "every attempt is recorded, not just the last"
        assert len({r.turn_id for r in new}) == 1, "one turn_id groups the attempts"
        assert [r.attempt for r in new] == [1, 2, 3]
        assert [r.gave_up for r in new] == [False, False, True], (
            "only the attempt that ended the turn is gave_up"
        )
        assert all(r.tool_name == "find_restaurants" for r in new)

    def test_a_recovered_turn_records_no_give_up(
        self, db, conversation, monkeypatch, rows,
    ):
        """The retry-recovery ratio depends on this: a turn_id with no gave_up row
        is one where retrying WORKED. That ratio is what says whether
        MAX_MALFORMED_RETRIES is set too low."""
        before = {r.id for r in rows()}
        stream = iter([
            _bad_request(),
            types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(tool_calls=None, content="Aur kuch chahiye?")
            )]),
        ])

        class _Completions:
            def create(self, **kwargs):
                item = next(stream)
                if isinstance(item, Exception):
                    raise item
                return item

        monkeypatch.setattr(
            agent, "_client",
            lambda: types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=_Completions())
            ),
        )

        agent.generate_reply(db, conversation)

        new = [r for r in rows() if r.id not in before]
        assert len(new) == 1
        assert new[0].gave_up is False, "a recovered turn must not look like a failure"
