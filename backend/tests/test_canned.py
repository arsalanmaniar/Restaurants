"""Canned replies follow the customer's language (followups-deferred #2).

These strings bypass the model, so SYSTEM_PROMPT's language rules never reach
them. Every one was hardcoded in a single language, and it broke in BOTH
directions: the fallback and prefilter replies were English-only, the payment
replacements Roman-Urdu-only.

Observed live. Conversation 724 was entirely Roman Urdu; the model malformed its
tool call, the salvage guard correctly refused to invent an answer, and the
customer received "Sorry, we're having a technical problem at the moment."
"""

import types

import httpx
import pytest
from groq import BadRequestError

from app.models import MessageDirection
from app.services import agent
from app.services import canned
from app.services import conversations as convo
from app.services import prefilter
from app.services.language import ENGLISH, ROMAN_URDU, UNKNOWN


def _completion(content):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(
            message=types.SimpleNamespace(tool_calls=None, content=content)
        )]
    )


def _bad_request():
    return BadRequestError(
        "tool_use_failed: could not parse tool call",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.groq.test")),
        body=None,
    )


class TestTheCatalogue:
    def test_every_key_exists_in_both_languages(self):
        """A key with one variant silently falls back to Roman Urdu for everyone,
        which is the bug this module exists to fix — reintroduced one key at a
        time."""
        missing = {
            key: [lang for lang in (ENGLISH, ROMAN_URDU) if lang not in canned._TEXTS[key]]
            for key in canned.KEYS
            if any(lang not in canned._TEXTS[key] for lang in (ENGLISH, ROMAN_URDU))
        }
        assert not missing, f"keys missing a language: {missing}"

    def test_the_two_languages_actually_differ(self):
        """Guard against a copy-paste that leaves both variants identical, which
        would pass every other test here while shipping one language."""
        same = [k for k in canned.KEYS
                if canned._TEXTS[k][ENGLISH] == canned._TEXTS[k][ROMAN_URDU]]
        assert not same, f"both variants identical for: {same}"

    def test_resolves_per_language(self):
        assert canned.text("fallback", ENGLISH) != canned.text("fallback", ROMAN_URDU)
        assert "technical" in canned.text("fallback", ENGLISH).lower()
        assert "technical masla" in canned.text("fallback", ROMAN_URDU).lower()

    def test_unknown_resolves_to_roman_urdu_not_english(self):
        """THE CONV 724 DEFAULT. services/language.py returns UNKNOWN often and by
        design; the old English-only constant meant UNKNOWN effectively meant
        English, and Roman Urdu speakers got an English apology."""
        for key in canned.KEYS:
            assert canned.text(key, UNKNOWN, order_number="AB-1", extra="") == \
                   canned.text(key, ROMAN_URDU, order_number="AB-1", extra="")

    def test_templated_keys_interpolate(self):
        for lang in (ENGLISH, ROMAN_URDU):
            assert "AB-9999" in canned.text("cod_order", lang, order_number="AB-9999")
            assert "AB-9999" in canned.text("order_placed", lang, order_number="AB-9999", extra="")

    def test_variants_returns_every_language(self):
        assert set(canned.variants("rate_limited")) == {
            canned._TEXTS["rate_limited"][ENGLISH],
            canned._TEXTS["rate_limited"][ROMAN_URDU],
        }


class TestRateLimitNoticeIsRecognisedInEitherLanguage:
    """prefilter.already_notified_rate_limit stops us telling the same customer to
    slow down on every message. It recognised the notice by EQUALITY against one
    hardcoded string — so once the notice is sent in the customer's language, a
    customer notified in the other one would never be recognised, and would be
    told to slow down on every single message. The exact spam the check prevents.
    """

    def _notified(self, db, conversation, notice):
        convo.log_message(db, conversation, MessageDirection.OUTBOUND, notice)
        db.flush()
        return prefilter.already_notified_rate_limit(db, conversation)

    def test_roman_urdu_notice_is_recognised(self, db, conversation):
        assert self._notified(db, conversation, canned.text("rate_limited", ROMAN_URDU))

    def test_english_notice_is_recognised(self, db, conversation):
        assert self._notified(db, conversation, canned.text("rate_limited", ENGLISH))

    def test_an_ordinary_reply_is_not_mistaken_for_the_notice(self, db, conversation):
        assert not self._notified(db, conversation, "Aapka order place ho gaya.")


class TestFallbackMatchesTheConversationLanguage:
    """End-to-end through handle_incoming_message, which is where conv 724 broke."""

    RU_HISTORY = ["mujhe biryani chahiye", "aap ke pass kya hai", "mujhe order karna hai"]
    EN_HISTORY = ["I would like to see the menu", "what do you have for me", "I want to order"]

    def _seed(self, db, conversation, messages):
        for text in messages:
            convo.log_message(db, conversation, MessageDirection.INBOUND, text)
        db.flush()

    def _fallback_for(self, db, conversation, monkeypatch, history):
        self._seed(db, conversation, history)

        stream = iter([_bad_request(), _bad_request(), _bad_request()])

        class _Completions:
            def create(self, **kwargs):
                raise next(stream)

        monkeypatch.setattr(
            agent, "_client",
            lambda: types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=_Completions())
            ),
        )
        sent = []
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))
        agent.handle_incoming_message(db, conversation, history[-1])
        assert len(sent) == 1
        return sent[0]

    def test_roman_urdu_conversation_gets_a_roman_urdu_fallback(
        self, db, conversation, monkeypatch,
    ):
        """CONV 724, REPLAYED. The malformed tool call still produces an honest
        fallback — now in the language the customer was writing."""
        reply = self._fallback_for(db, conversation, monkeypatch, self.RU_HISTORY)

        assert reply == canned.text("fallback", ROMAN_URDU)
        assert "Sorry, we're having a technical problem" not in reply

    def test_english_conversation_gets_an_english_fallback(
        self, db, conversation, monkeypatch,
    ):
        reply = self._fallback_for(db, conversation, monkeypatch, self.EN_HISTORY)

        assert reply == canned.text("fallback", ENGLISH)

    def test_the_histories_are_actually_decisive(self, db, conversation):
        """Guard the guard: if the classifier read these as UNKNOWN, both tests
        above would pass for the wrong reason — UNKNOWN resolves to Roman Urdu, so
        the English test would be asserting nothing."""
        from app.services import language as language_service

        assert language_service.customer_language(self.RU_HISTORY) == ROMAN_URDU
        assert language_service.customer_language(self.EN_HISTORY) == ENGLISH


class TestPaymentRepliesFollowTheLanguageToo:
    """The mirror-image failure: these were Roman-Urdu-only, so an English
    customer hitting the payment guards got Urdu."""

    def test_no_order_reply_in_both_languages(self, db, conversation):
        assert agent._payment_switch_reply(None, ENGLISH) == canned.text("no_order", ENGLISH)
        assert agent._payment_switch_reply(None, ROMAN_URDU) == canned.text("no_order", ROMAN_URDU)

    def test_cod_reply_names_the_order_in_both_languages(self, db, cod_order, conversation):
        for lang in (ENGLISH, ROMAN_URDU):
            reply = agent._payment_switch_reply(cod_order, lang)
            assert cod_order.order_number in reply

    def test_order_report_in_both_languages(self):
        trace = [{"tool": "place_order",
                  "result": {"order_number": "AB-123456", "total": "980.00"}}]
        english = agent._order_report(trace, ENGLISH)
        urdu = agent._order_report(trace, ROMAN_URDU)
        assert "AB-123456" in english and "AB-123456" in urdu
        assert "980.00" in english and "980.00" in urdu
        assert english != urdu
