"""Conversation-replay regression wall.

Each test below replays a REAL bug transcript — a fixed sequence of customer
messages — through the actual tool loop (`handle_incoming_message`), with the
Groq model mocked to a scripted sequence of responses and the tools running for
real against the seeded test database. The assertion is on the OUTBOUND text the
customer would actually receive (or, where the fix is tool-level, on the resulting
conversation state).

Why this exists: the per-feature tests prove each guard in isolation. This suite
proves the end-to-end path — prefilter, tool loop, every guard, send — still turns
each of these specific real-world transcripts into a safe reply. It is the wall
that must stay green before any deploy, so none of these can silently regress.

The transcripts:
  1. Tax bait-and-switch (Bug 1)       — subtotal read back as the Total.
  2. Post-COD payment switch (Bug 2)    — "pay online?" after a COD order, ignored.
  3. Burger zero-match (Bug 3, Part A)  — "no burgers" followed by a restaurant list.
  4. Shortlist selection (pre-session)  — naming a shortlisted restaurant reset the
                                          conversation instead of selecting it. The
                                          seed has no "Mandi House"; Karachi Biryani
                                          House plays that role, exactly as
                                          test_shortlist_continuity.py does.

Regression semantics of a scripted replay: the model's turns are fixed, so what
protects each transcript is that a GUARD (or the tool) changes the outcome. If a
guard were removed, the scripted "bad" draft would reach the customer / the tool
would reset — and the assertion would fail. That is exactly the regression we want
to catch.
"""

import json
import types
from decimal import Decimal

import pytest

from app.models import MessageDirection
from app.services import agent
from app.services import conversations as convo


# --------------------------------------------------------------------------- #
# Scripted-model completion builders (Groq response shape)
# --------------------------------------------------------------------------- #


def _msg(tool_calls=None, content=None):
    return types.SimpleNamespace(tool_calls=tool_calls, content=content)


def _tc(call_id, name, args):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _completion(message):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


# --------------------------------------------------------------------------- #
# The multi-turn replay harness
# --------------------------------------------------------------------------- #


class Replay:
    """Drives a conversation turn by turn. Each `turn` logs the inbound (as the
    webhook would), feeds the scripted model responses for that turn, runs the real
    `handle_incoming_message`, and returns the text actually sent to the customer.
    Tool calls execute for real against the seeded DB, so state (cart, active
    restaurant, orders, remembered shortlist) accumulates across turns."""

    def __init__(self, db, conversation, monkeypatch):
        self.db = db
        self.conversation = conversation
        self.sent: list[str] = []
        self._queue: list = []
        driver = self

        class _Completions:
            def create(self, **kwargs):
                assert driver._queue, "replay ran out of scripted model responses"
                return driver._queue.pop(0)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_Completions())
        )
        monkeypatch.setattr(agent, "_client", lambda: client)
        monkeypatch.setattr(agent, "send_text", lambda to, body: driver.sent.append(body))

    def turn(self, inbound: str, model_responses: list) -> str:
        self._queue.extend(model_responses)
        # The webhook logs the inbound before handing off; mirror that so it is part
        # of the history the model sees.
        convo.log_message(self.db, self.conversation, MessageDirection.INBOUND, inbound)
        before = len(self.sent)
        agent.handle_incoming_message(self.db, self.conversation, inbound)
        assert len(self.sent) > before, "no outbound was sent for this turn"
        assert not self._queue, "scripted responses left unconsumed — the turn took a different path"
        return self.sent[-1]


@pytest.fixture
def replay(db, conversation, monkeypatch):
    return Replay(db, conversation, monkeypatch)


# --------------------------------------------------------------------------- #
# The transcripts
# --------------------------------------------------------------------------- #


class TestReplayRegressions:

    def test_1_tax_bait_and_switch_read_back_is_corrected(self, replay, pizza, menu_item):
        """Bug 1. Cart = one Chicken Tikka Pizza (Rs. 1150). The model reads the bare
        subtotal back as the Total; the customer must instead be quoted the taxed
        total (1150 + 15% COD tax + Rs. 100 delivery = Rs. 1422.50)."""
        # Turn 1 — build the cart through the loop (real get_menu + add_to_cart).
        replay.turn(
            "ek chicken tikka pizza order karni hai",
            [
                _completion(_msg(tool_calls=[
                    _tc("t1", "get_menu", {"restaurant_id": pizza.id}),
                    _tc("t2", "add_to_cart", {"menu_item_id": menu_item.id, "quantity": 1}),
                ])),
                _completion(_msg(content="Pizza cart mein add kar di. Confirm karun?")),
            ],
        )

        # Turn 2 — the bug: subtotal quoted as the Total, then the guard's correction.
        understated = "Aapka order:\n1x Chicken Tikka Pizza\nTotal: Rs. 1150\nConfirm karein?"
        corrected = (
            "Aapka order:\n1x Chicken Tikka Pizza\n"
            "Subtotal: Rs. 1150\nTax: Rs. 172.50\nDelivery: Rs. 100.00\n"
            "Total: Rs. 1422.50\nConfirm karein?"
        )
        outbound = replay.turn(
            "haan confirm, cash on delivery",
            [
                _completion(_msg(content=understated)),
                _completion(_msg(tool_calls=[_tc("p", "preview_bill", {"payment_method": "cod"})])),
                _completion(_msg(content=corrected)),
            ],
        )

        assert outbound == corrected
        assert agent._quoted_total(outbound) == Decimal("1422.50"), (
            "the customer must be quoted the tax-inclusive total, never the bare subtotal"
        )

    def test_2_post_cod_online_request_is_answered(self, replay, cod_order):
        """Bug 2. A COD order is already placed. The customer asks to pay online and
        the model ignores it; the customer must get the committed-to-COD / place-a-
        new-online-order answer instead of a non-response."""
        outbound = replay.turn(
            "online pay kar sakta hoon?",
            [_completion(_msg(content="Aapka order jald hi deliver ho jayega."))],
        )
        assert outbound == agent.FAKE_LINK_REPLACEMENT
        assert "jald hi deliver" not in outbound

    def test_3_burger_zero_match_is_not_contradicted(self, replay):
        """Bug 3, Part A. burger is a genuine zero-match. The model lists restaurants
        anyway ('no burgers' + a restaurant list); the customer must instead be told
        it isn't available and offered cuisines, with no restaurant named."""
        bad = "Burger nahi hai.\n1. Karachi Biryani House\n2. Pizza Junction"
        corrected = (
            "Burger available nahi hai. Humare paas Desi, Pizza aur Chinese hai — "
            "kaunsi try karein?"
        )
        outbound = replay.turn(
            "burger khana hai",
            [
                _completion(_msg(tool_calls=[_tc("f", "find_restaurants", {"query": "burger"})])),
                _completion(_msg(content=bad)),
                _completion(_msg(content=corrected)),
            ],
        )
        assert outbound == corrected
        for name in ("Karachi Biryani House", "Pizza Junction", "Wok & Roll"):
            assert name not in outbound, f"a zero-match reply must not name {name!r}"

    def test_4_shortlist_selection_does_not_reset(self, replay, biryani):
        """The pre-session shortlist bug. After a biryani shortlist, naming one of its
        restaurants inside a question is a SELECTION, not a fresh search that resets
        the conversation. The fix is tool-level, so the regression assertion is on
        state: the selection sets the active restaurant (a reset would leave it None)."""
        # Turn 1 — dish query builds and remembers the shortlist.
        replay.turn(
            "biryani hai?",
            [
                _completion(_msg(tool_calls=[_tc("f1", "find_restaurants", {"query": "biryani"})])),
                _completion(_msg(content="Biryani serve karne wale:\n1. Karachi Biryani House\n\nKaunsa chahenge?")),
            ],
        )

        # Turn 2 — naming the shortlisted restaurant inside a question.
        replay.turn(
            "Karachi Biryani House per hoti h biryani?",
            [
                _completion(_msg(tool_calls=[_tc(
                    "f2", "find_restaurants",
                    {"query": "Karachi Biryani House per hoti h biryani?"},
                )])),
                _completion(_msg(content="Karachi Biryani House ka menu — Chicken Biryani available hai. Order karun?")),
            ],
        )

        assert replay.conversation.active_restaurant_id == biryani.id, (
            "naming a shortlisted restaurant must SELECT it, not reset to a generic search"
        )
        assert replay.conversation.context.get("shown_menu_restaurant") == biryani.name
