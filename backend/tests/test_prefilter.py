"""The pre-filter that stops obviously-not-a-food-order traffic from burning Groq
tokens. Every case here corresponds to real production traffic we watched blow
past the free-tier quota (see prefilter module docstring for the incidents)."""

import time
import types

import pytest

from app.services import agent, prefilter


# --------------------------------------------------------------------------- #
# is_offtopic — pure function, no DB
# --------------------------------------------------------------------------- #


class TestIsOfftopic:
    def test_legitimate_short_messages_pass(self):
        for msg in [
            "hi",
            "Hello",
            "Karachi biryani Ka batao",
            "Yes 1 Chicken biryani",
            "Not only chicken biryani",
            "Saddar Karachi",
            "Please make it extra spicy and no onions",
        ]:
            assert not prefilter.is_offtopic(msg), f"real customer message rejected: {msg!r}"

    def test_empty_or_none_passes_through(self):
        # An empty body is handled by webhooks earlier — here we just make sure
        # it does not itself trigger the off-topic path.
        assert not prefilter.is_offtopic("")
        assert not prefilter.is_offtopic(None)

    def test_url_is_offtopic(self):
        """conv#645: auto-forwarded job listings with URLs burned tokens
        producing a generic 'we have three restaurants' reply."""
        assert prefilter.is_offtopic(
            "Beechtree Jobs July 2026 https://www.careerjoin.com/beechtree-jobs-july-2026/"
        )
        assert prefilter.is_offtopic("https://youtu.be/Dn-cXCj-z3I")
        assert prefilter.is_offtopic("check this http://example.com")

    def test_long_message_is_offtopic(self):
        """Real customer messages top out around 200 chars. Anything longer is
        overwhelmingly a WhatsApp forward or a broadcast."""
        assert prefilter.is_offtopic("x" * (prefilter.MAX_LEGITIMATE_LENGTH + 1))
        # exactly at the limit is still allowed
        assert not prefilter.is_offtopic("x" * prefilter.MAX_LEGITIMATE_LENGTH)

    def test_broadcast_forward_is_offtopic(self):
        """conv#647: news forward with three *bold headlines*."""
        forward = (
            "*Breaking news headline one.* "
            "*Another important development.* "
            "*Third headline for context.*"
        )
        assert prefilter.is_offtopic(forward)

    def test_two_bold_markers_still_allowed(self):
        """A customer emphasising two words should not be misclassified."""
        assert not prefilter.is_offtopic("please *no onions* and *extra spicy*")

    def test_mlm_forward_is_offtopic(self):
        """conv#644: MLM investment forward, ~400 chars of *bold* markers."""
        mlm = (
            "*Earning Ads Watching Package* "
            "*4 ads daily* "
            "*Basic 950 Rupees* "
            "Investment plan with profit — daily ads earning ranges vary."
        )
        assert prefilter.is_offtopic(mlm)


# --------------------------------------------------------------------------- #
# rate-limit — needs a DB
# --------------------------------------------------------------------------- #


def _log_inbound(db, conversation, body="hi", provider_id_suffix=""):
    """Write an INBOUND row the same shape the webhook produces."""
    from app.services import conversations as convo

    convo.log_message(
        db,
        conversation,
        agent.MessageDirection.INBOUND,
        body,
        meta=None,
        provider_message_id=f"test_{time.time_ns()}{provider_id_suffix}",
    )
    db.flush()


class TestRateLimit:
    def test_under_limit_is_not_rate_limited(self, db, conversation):
        for _ in range(prefilter.MAX_MESSAGES_PER_MINUTE):
            _log_inbound(db, conversation)
        assert not prefilter.is_rate_limited(db, conversation)

    def test_over_limit_is_rate_limited(self, db, conversation):
        for _ in range(prefilter.MAX_MESSAGES_PER_MINUTE + 1):
            _log_inbound(db, conversation)
        assert prefilter.is_rate_limited(db, conversation)

    def test_old_messages_do_not_count(self, db, conversation, monkeypatch):
        """A customer who chatted a lot yesterday is not permanently silenced —
        the window slides. Pin the current time to 'now' and log all messages
        as being from 2 minutes ago, then verify the limit is not tripped."""
        from datetime import datetime, timedelta, timezone

        two_min_ago = datetime.now(timezone.utc) - timedelta(minutes=2)
        for _ in range(prefilter.MAX_MESSAGES_PER_MINUTE + 5):
            _log_inbound(db, conversation)
        # Rewrite the timestamps so they fall outside the window.
        from app.models import MessageLog
        from sqlalchemy import update

        db.execute(
            update(MessageLog)
            .where(MessageLog.conversation_id == conversation.id)
            .values(created_at=two_min_ago)
        )
        db.flush()

        assert not prefilter.is_rate_limited(db, conversation)

    def test_already_notified_flag_flips_on_recent_notice(self, db, conversation):
        from app.services import conversations as convo

        # No outbound yet → false
        assert not prefilter.already_notified_rate_limit(db, conversation)

        # Some other reply → false
        convo.log_message(
            db, conversation, agent.MessageDirection.OUTBOUND, "Menu is..."
        )
        db.flush()
        assert not prefilter.already_notified_rate_limit(db, conversation)

        # Rate-limit notice as last outbound → true
        convo.log_message(
            db, conversation, agent.MessageDirection.OUTBOUND, prefilter.RATE_LIMITED_REPLY
        )
        db.flush()
        assert prefilter.already_notified_rate_limit(db, conversation)


# --------------------------------------------------------------------------- #
# integration — handle_incoming_message must short-circuit before Groq
# --------------------------------------------------------------------------- #


@pytest.fixture
def exploding_groq(monkeypatch):
    """If any test in this module ever reaches Groq, blow up loudly — the whole
    point of the pre-filter is that it never does."""

    class MustNotBeCalled:
        def create(self, **kwargs):
            raise AssertionError(
                "prefilter should have short-circuited BEFORE Groq was contacted"
            )

    class Client:
        chat = types.SimpleNamespace(completions=MustNotBeCalled())

    monkeypatch.setattr(agent, "_client", lambda: Client())


@pytest.fixture
def sent(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(agent, "send_text", lambda to, body: captured.append(body))
    return captured


class TestHandleIncomingShortCircuits:
    def test_offtopic_message_never_reaches_groq(
        self, db, conversation, exploding_groq, sent
    ):
        body = "check this out https://youtu.be/Dn-cXCj-z3I"
        _log_inbound(db, conversation, body=body)

        agent.handle_incoming_message(db, conversation, body)

        assert sent == [prefilter.OFFTOPIC_REDIRECT]

    def test_rate_limited_first_hit_gets_the_notice(
        self, db, conversation, exploding_groq, sent
    ):
        for _ in range(prefilter.MAX_MESSAGES_PER_MINUTE + 1):
            _log_inbound(db, conversation)

        agent.handle_incoming_message(db, conversation, "still typing")

        assert sent == [prefilter.RATE_LIMITED_REPLY]

    def test_rate_limited_sustained_burst_stays_silent(
        self, db, conversation, exploding_groq, sent
    ):
        """A spammer sending 100 messages should get 'please slow down' ONCE,
        not 100 times — otherwise the bot mirrors the spam back at itself and
        burns Wassender quota."""
        from app.services import conversations as convo

        for _ in range(prefilter.MAX_MESSAGES_PER_MINUTE + 1):
            _log_inbound(db, conversation)
        # Simulate we already sent the rate-limit notice a moment ago
        convo.log_message(
            db, conversation, agent.MessageDirection.OUTBOUND, prefilter.RATE_LIMITED_REPLY
        )
        db.flush()

        agent.handle_incoming_message(db, conversation, "still typing")

        assert sent == [], "must stay silent after the notice was already delivered"
