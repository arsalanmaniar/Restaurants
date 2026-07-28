"""Reply-language matching — services/language.py + the agent guard.

The prompt has always said "match the customer's language". Production shows it
drifts: an unambiguous English "How are you" was answered with a Roman Urdu
greeting (conv 722), while the same class of opener got English elsewhere
(conv 713).

The risk in fixing it is over-detection, not under-detection. Pakistani customers
write Roman Urdu full of English loanwords, and a naive classifier reads those as
English. A first scan of real traffic flagged 7 "mismatches" of which only ONE was
genuine — acting on the rest would have broken four correct turns in conv 721.
Every string below marked "real:" is taken verbatim from production.
"""

import pytest

from app.services import agent
from app.services import language as L
from tests.test_agent_guards import completion, message, scripted_model  # noqa: F401


class TestClassify:

    @pytest.mark.parametrize("text", [
        "How are you",                                  # real: conv 722, the genuine violation
        "Do you have any pizza available right now",
        "I would like to order the chicken pizza",
    ])
    def test_clear_english(self, text):
        assert L.classify(text) == L.ENGLISH

    @pytest.mark.parametrize("text", [
        "Aik kam karo order cancel kardo ok",           # real: conv 711
        "online kya hai ya cash ?",                     # real: conv 721
        "Apny Kha na Haleem h",                         # real: conv 715
        "mujhe biryani chahiye aur soft drink",
    ])
    def test_clear_roman_urdu(self, text):
        assert L.classify(text) == L.ROMAN_URDU

    @pytest.mark.parametrize("text", [
        "Hi", "Hello", "Hey",                           # real: shared by both languages
        "Yes", "yes", "ok", "Yes confirm h",            # real: conv 721 / 710
        "wok and roll",                                 # real: a restaurant NAME, not language
        "Nihari khani h",                               # real: conv 722
        "Hoti market saadar Karachi",                   # real: conv 713 — an ADDRESS
        "House 5, DHA Phase 6, Lahore",
        "COD", "923001234567", "",
    ])
    def test_indecisive_input_is_unknown(self, text):
        """Everything here decides nothing. Each one is a false positive that would
        have suppressed a correct reply."""
        assert L.classify(text) == L.UNKNOWN

    def test_loanwords_are_never_english_evidence(self):
        """The single most important property: a Roman Urdu sentence built from
        loanwords must not read as English."""
        assert L.classify("order confirm karo please") != L.ENGLISH

    def test_a_lone_urdu_marker_is_not_enough(self):
        """MIN_MARGIN: 'Hoti market' trips exactly one marker ('hoti'). Without the
        margin, giving an address would flip an English conversation to Roman Urdu."""
        assert L.classify("Hoti market saadar Karachi") == L.UNKNOWN


class TestCustomerLanguage:

    def test_most_recent_decisive_message_wins(self):
        assert L.customer_language([
            "mujhe biryani chahiye aur soft drink",
            "Do you have any pizza available right now",
        ]) == L.ENGLISH

    def test_a_short_yes_does_not_flip_an_urdu_conversation(self):
        """real: conv 721 — four consecutive "Yes"/"yes" turns inside a Roman Urdu
        conversation, each correctly answered in Roman Urdu. A per-message rule
        would have broken every one of them."""
        assert L.customer_language([
            "mujhe biryani chahiye aur soft drink", "Yes", "yes", "Yes",
        ]) == L.ROMAN_URDU

    def test_all_indecisive_history_is_unknown(self):
        """real: conv 721's opening — nothing decisive, so the guard stays silent."""
        assert L.customer_language(["wok and roll", "yes", "Yes"]) == L.UNKNOWN

    def test_empty_history(self):
        assert L.customer_language([]) == L.UNKNOWN


class TestReplyMismatch:
    # real: conv 722's outbound, sent to a customer who wrote "How are you"
    URDU_GREETING = (
        "AbhiAya mein khush amdeed. Available restaurants:\n"
        "1. Karachi Biryani House\n2. Pizza Junction\n\n"
        "Aap kis restaurant se order karna chahenge?"
    )
    ENGLISH_GREETING = (
        "Welcome to AbhiAya.\n\nAvailable restaurants:\n"
        "1. Karachi Biryani House\n2. Pizza Junction\n\n"
        "Which restaurant would you like to order from?"
    )

    def test_the_real_production_violation_is_caught(self):
        assert L.reply_mismatches(L.ENGLISH, self.URDU_GREETING) is True

    def test_matching_languages_pass(self):
        assert L.reply_mismatches(L.ROMAN_URDU, self.URDU_GREETING) is False
        assert L.reply_mismatches(L.ENGLISH, self.ENGLISH_GREETING) is False

    def test_unknown_customer_language_never_fires(self):
        """A "Hi"-only conversation has no signal — never suppress on a guess."""
        assert L.reply_mismatches(L.UNKNOWN, self.URDU_GREETING) is False
        assert L.reply_mismatches(L.UNKNOWN, self.ENGLISH_GREETING) is False

    def test_an_indecisive_reply_never_fires(self):
        """Short replies ("Theek hai!") carry no signal either."""
        assert L.reply_mismatches(L.ENGLISH, "Theek hai!") is False
        assert L.reply_mismatches(L.ROMAN_URDU, "Done!") is False


class TestAgentGuard:

    def _inbound(self, db, conversation, text):
        from app.models import MessageDirection
        from app.services import conversations as convo
        convo.log_message(db, conversation, MessageDirection.INBOUND, text)
        db.flush()

    def test_guard_fires_on_the_real_production_mismatch(self, db, conversation):
        self._inbound(db, conversation, "How are you")
        assert agent._replies_in_the_wrong_language(
            db, conversation, TestReplyMismatch.URDU_GREETING
        ) is True

    def test_guard_silent_when_languages_match(self, db, conversation):
        self._inbound(db, conversation, "How are you")
        assert agent._replies_in_the_wrong_language(
            db, conversation, TestReplyMismatch.ENGLISH_GREETING
        ) is False

    def test_guard_silent_on_a_bare_greeting(self, db, conversation):
        """Decision: a bare "Hi" defaults to Roman Urdu and must NOT be treated as
        an English signal, so a Roman Urdu reply is correct and stays."""
        self._inbound(db, conversation, "Hi")
        assert agent._replies_in_the_wrong_language(
            db, conversation, TestReplyMismatch.URDU_GREETING
        ) is False

    def test_guard_silent_for_short_confirmations_in_an_urdu_conversation(
        self, db, conversation,
    ):
        """real: conv 721. The four correct turns a naive guard would have broken."""
        self._inbound(db, conversation, "mujhe biryani chahiye aur soft drink")
        self._inbound(db, conversation, "Yes")
        assert agent._replies_in_the_wrong_language(
            db, conversation, TestReplyMismatch.URDU_GREETING
        ) is False

    def test_wrong_language_reply_is_regenerated_end_to_end(
        self, db, conversation, scripted_model,
    ):
        """The customer wrote English; the model answers in Roman Urdu; the guard
        regenerates and the customer receives English.

        The replies deliberately contain NO numbered restaurant list and NO prices,
        so nothing but the language guard can act on them. An earlier version used
        the greeting and passed even with the language guard disabled — the
        ungrounded-list guard was silently doing the work."""
        urdu = "Ji haan, hum aap ki madad kar sakte hain. Aap kya order karna chahenge?"
        english = "Yes, we can help you with that. What would you like to order?"
        self._inbound(db, conversation, "How are you")
        scripted_model([
            completion(message(content=urdu)),
            completion(message(content=english)),
        ])
        reply, _trace = agent.generate_reply(db, conversation)
        assert reply == english
