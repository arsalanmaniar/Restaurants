"""The guards that stop the model hurting a customer.

The Groq client is stubbed, so these run with no API key, no network, and no token cost —
and, more importantly, they are deterministic. Each one replays a failure the real model
actually produced during development.
"""

import json
import types
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models import OrderStatus, PaymentMethod
from app.services import agent
from app.services import conversations as convo
from app.services import grounding
from app.services import billing
from app.services import tools


# --------------------------------------------------------------------------- #
# The three narrow guards below were folded into services/grounding.py. Their
# tests are kept verbatim — each encodes a real production failure — and simply
# re-pointed at the single auditor entry point through these shims.
# --------------------------------------------------------------------------- #


def _violation(db, conversation, reply, trace):
    return grounding.audit(db, conversation, reply, trace, check_prices=False)


def _bill_unbacked(reply, trace):
    """Old agent._readback_bill_is_unbacked."""
    v = grounding._bill_violation(reply or "", trace)
    return v is not None and v.kind == "unbacked_bill"


def _kind(db, conversation, reply, trace):
    """Old agent._discovery_padding -> True when the auditor reports padding."""
    v = _violation(db, conversation, reply, trace)
    return v is not None and v.kind in ("search_padding", "unlisted_offer")


def _list_kind(db, conversation, reply, trace):
    """Old agent._ungrounded_restaurant_list."""
    v = _violation(db, conversation, reply, trace)
    return v is not None and v.kind == "unlisted_offer"


def _subtotal(conversation):
    return sum(
        Decimal(line["price"]) * line["quantity"]
        for line in conversation.cart["items"]
    )


def message(tool_calls=None, content=None):
    return types.SimpleNamespace(tool_calls=tool_calls, content=content)


def tool_call(call_id, name, args):
    return types.SimpleNamespace(
        id=call_id,
        function=types.SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def completion(msg):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


@pytest.fixture
def scripted_model(monkeypatch):
    """Make the model return a fixed sequence of responses."""

    def install(responses):
        stream = iter(responses)
        calls = {"count": 0}

        class Completions:
            def create(self, **kwargs):
                calls["count"] += 1
                return next(stream)

        class Client:
            chat = types.SimpleNamespace(completions=Completions())

        monkeypatch.setattr(agent, "_client", lambda: Client())
        return calls

    return install


class TestDuplicateToolCallGuard:
    def test_identical_add_to_cart_in_one_turn_does_not_double_the_food(
        self, db, conversation, pizza, menu_item, scripted_model
    ):
        """The model was observed issuing the SAME add_to_cart twice in a single turn —
        when the customer merely ASKED 'how much is the total?'. That silently doubled
        their order."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        db.flush()

        args = {"menu_item_id": menu_item.id, "quantity": 2}
        calls = scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call("a", "add_to_cart", args),
                            tool_call("b", "add_to_cart", args),   # identical
                        ]
                    )
                ),
                completion(message(content="Added to your cart!")),
            ]
        )

        reply, trace = agent.generate_reply(db, conversation)

        assert calls["count"] > 0, "the model must actually have been called"
        assert len(trace) == 2, "both calls are traced"

        quantities = [line["quantity"] for line in conversation.cart["items"]]
        assert quantities == [2], "the duplicate was collapsed, not applied twice"

    def test_different_items_are_not_collapsed(
        self, db, conversation, pizza, menu_item, scripted_model
    ):
        menu = tools.get_menu(db, conversation, restaurant_id=pizza.id)
        other = next(i["id"] for i in menu["items"] if i["id"] != menu_item.id)
        db.flush()

        scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call("a", "add_to_cart", {"menu_item_id": menu_item.id}),
                            tool_call("b", "add_to_cart", {"menu_item_id": other}),
                        ]
                    )
                ),
                completion(message(content="Done!")),
            ]
        )

        agent.generate_reply(db, conversation)
        assert len(conversation.cart["items"]) == 2


class TestLeakedToolCallIsNeverSent:
    LEAK = '{"type": "function", "name": "get_menu", "parameters": {"restaurant_id": "Pizza Junction"}}'
    # Exact string that reached a real customer in conv#634 during live testing.
    CONV_634_LEAK = (
        '{"type": "function", "name": "add_to_cart", '
        '"parameters": {"menu_item_id": "429", "quantity": "2"}}'
    )

    def test_detector(self):
        assert agent._leaks_tool_call(self.LEAK)
        assert agent._leaks_tool_call(self.CONV_634_LEAK)
        # OpenAI/Groq response-format shape (arguments, not parameters):
        assert agent._leaks_tool_call('{"name": "get_menu", "arguments": {"restaurant_id": 3}}')
        # Qwen-style XML wrapper — will bite us if we ever swap model:
        assert agent._leaks_tool_call('<tool_call>{"name":"get_menu"}</tool_call>')
        assert agent._leaks_tool_call("<function=get_menu {}></function>")
        # Embedded in prose is still a leak — the raw JSON must not reach the customer:
        assert agent._leaks_tool_call(
            'Let me check that. {"type":"function","name":"get_menu","parameters":{}}'
        )
        # Legitimate replies must not trigger the gate:
        assert not agent._leaks_tool_call("Your order is on its way! Total Rs. 2780")
        assert not agent._leaks_tool_call("What's your name?")

    def test_customer_never_receives_raw_json(self, db, conversation, scripted_model,
                                              monkeypatch):
        """The model printed its tool call as prose and the customer received raw JSON.
        This must be impossible from any code path."""
        scripted_model([completion(message(content=self.LEAK))] * 4)

        sent = []
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))

        agent.handle_incoming_message(db, conversation, "show me the menu")

        assert sent, "something must be sent"
        assert not agent._leaks_tool_call(sent[0])
        assert sent[0] == agent.FALLBACK_REPLY

    def test_generate_reply_never_returns_raw_json_even_when_model_persists(
        self, db, conversation, scripted_model
    ):
        """The gate at the outbound edge (handle_incoming_message) is not enough — any
        caller that uses generate_reply directly (test drivers, batch jobs, an admin
        replay tool) could receive raw JSON. This is what actually happened in conv#634.
        generate_reply must be self-defending: after the forced-retry, if the model is
        still leaking, the returned text must not be raw JSON."""
        # Two leaked replies in a row: the first triggers the forced-tool retry, the
        # second (post-retry) would previously fall through and return the leak.
        scripted_model([completion(message(content=self.CONV_634_LEAK))] * 4)

        reply, _trace = agent.generate_reply(db, conversation)

        assert not agent._leaks_tool_call(reply), (
            f"generate_reply returned raw tool-call JSON to its caller: {reply!r}"
        )
        assert reply == agent.FALLBACK_REPLY


class TestNoArgToolCalls:
    def test_null_arguments_are_treated_as_empty(self, db, conversation, cod_order,
                                                 scripted_model):
        """No-arg calls arrive as the literal `null`, not `{}`. Unhandled, this hit
        `**None` and every get_order_status() failed on its first attempt."""
        call = types.SimpleNamespace(
            id="a",
            function=types.SimpleNamespace(name="get_order_status", arguments="null"),
        )
        scripted_model(
            [
                completion(message(tool_calls=[call])),
                completion(message(content="Your order is on the way.")),
            ]
        )

        _reply, trace = agent.generate_reply(db, conversation)

        assert "error" not in trace[0]["result"], trace[0]["result"]
        assert trace[0]["result"]["order_number"] == cod_order.order_number


class TestFailureHandling:
    def test_groq_failure_produces_a_safe_reply(self, db, conversation, monkeypatch):
        from groq import GroqError

        # handle_incoming_message rolls back when Groq fails. Commit the conversation
        # first so the rollback doesn't discard the fixture's own rows — in production the
        # webhook has already committed the customer and the inbound message by this point.
        db.commit()

        class Exploding:
            def create(self, **kwargs):
                raise GroqError("service unavailable")

        class Client:
            chat = types.SimpleNamespace(completions=Exploding())

        monkeypatch.setattr(agent, "_client", lambda: Client())

        sent = []
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))

        agent.handle_incoming_message(db, conversation, "hi")

        assert sent == [agent.FALLBACK_REPLY], "the customer must not be left in silence"


class TestFakeLinkDetector:
    """conv 690 row #653: after a COD order was placed, customer asked for online
    payment; the model narrated "link bhej diya gaya hai" without ever calling
    place_order or including a URL. The post-gen guard must catch this and
    replace the reply with a corrective fallback."""

    def test_fake_link_claim_without_url_gets_replaced(
        self, db, conversation, scripted_model
    ):
        # Model produces text claiming a link was sent — no tool call, no URL.
        scripted_model(
            [
                completion(
                    message(
                        content=(
                            "Aapko payment link bhej diya gaya hai, aap online "
                            "payment kar sakte hain. 🚚"
                        )
                    )
                )
            ]
        )
        sent = []
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))
        try:
            agent.handle_incoming_message(db, conversation, "online payment karni h")
        finally:
            monkeypatch.undo()

        assert len(sent) == 1
        delivered = sent[0]
        assert "link bhej" not in delivered.lower(), (
            f"the fake link claim must be suppressed, but customer got: {delivered!r}"
        )
        assert "Cash on Delivery" in delivered
        assert "naya order" in delivered.lower() or "new order" in delivered.lower()

    def test_real_place_order_link_passes_through(
        self, db, conversation, scripted_model
    ):
        """When place_order legitimately returned a payment_link (real prepaid
        order flow), a reply that includes the URL must NOT be suppressed."""
        # Simulate: model reply mentions "payment link" AND includes an https URL,
        # and the trace shows a place_order with a payment_link field. Since we
        # can't easily drive a real place_order in this stub, poke the guard
        # helper directly.
        trace = [
            {
                "tool": "place_order",
                "args": "{}",
                "result": {
                    "order_number": "AB-XXXXXX",
                    "payment_link": "https://example.test/pay/abc",
                },
            }
        ]
        reply = "Payment link: https://example.test/pay/abc — tap to pay."
        assert not agent._claims_fake_link(reply, trace), (
            "a real link + real trace must not be treated as a fake claim"
        )

    def test_reply_with_no_link_talk_is_not_flagged(self, db, conversation):
        """A regular reply that never mentions payment links must pass through."""
        trace: list[dict] = []
        reply = "Aapka order confirm kar du? Total: Rs. 500. Haan ya nahi?"
        assert not agent._claims_fake_link(reply, trace)


class TestLoopDetection:
    """A read-only tool returning the same result twice in one turn is a sign the
    model is confused (e.g. list_restaurants returns 1 dead-end restaurant and it
    calls again hoping for a different answer). Verified in prod as conv 690 which
    looped "Available: 1. Mandi House" 5 turns. Nudge once per turn."""

    def test_read_only_repeat_injects_nudge(
        self, db, conversation, pizza, scripted_model
    ):
        """Two consecutive list_restaurants calls with identical result → the tool loop
        should append a "try something different" system message before continuing.
        We can't inspect the injected message from outside generate_reply, so verify
        indirectly: on the third round the model gets a chance to see the nudge and
        can pivot to a different tool."""
        # First round: model calls list_restaurants
        # Second round: model calls list_restaurants AGAIN (identical result → nudge)
        # Third round: model calls get_menu (a different tool)
        # Fourth round: model produces text.
        scripted_model(
            [
                completion(message(tool_calls=[tool_call("a", "list_restaurants", {})])),
                completion(message(tool_calls=[tool_call("b", "list_restaurants", {})])),
                completion(
                    message(tool_calls=[tool_call("c", "get_menu", {"restaurant_id": pizza.id})])
                ),
                completion(message(content="Here's the menu.")),
            ]
        )

        reply, trace = agent.generate_reply(db, conversation)

        # Both list_restaurants calls happened AND we didn't hit MAX_TOOL_ROUNDS
        # (nudge fires as a system message, not a tool result — it doesn't consume
        # a round budget itself).
        assert reply == "Here's the menu."
        assert [t["tool"] for t in trace] == [
            "list_restaurants",
            "list_restaurants",
            "get_menu",
        ]

    def test_mutating_tool_repeat_does_not_nudge(
        self, db, conversation, pizza, menu_item, scripted_model
    ):
        """add_to_cart / place_order have their own dedup guard (MUTATING_TOOLS).
        The loop-detect nudge is only for read-only tools — mutating repeats are
        handled separately and shouldn't also trigger the nudge."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        db.flush()

        args = {"menu_item_id": menu_item.id, "quantity": 1}
        scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call("a", "add_to_cart", args),
                            tool_call("b", "add_to_cart", args),  # dedup'd by MUTATING_TOOLS guard
                        ]
                    )
                ),
                completion(message(content="Added.")),
            ]
        )

        reply, trace = agent.generate_reply(db, conversation)
        assert reply == "Added."


class TestReadbackBillGuard:
    """Reply layer: any BILL the model shows must have been produced by a preview_bill
    call this turn (Bug 1, the AB-F6DF70 fabricated bill, and Issue 3's zero-tax bill).
    A number the model computed itself — a bare subtotal, an invented rate/delivery, or
    a Rs. 0 tax — has no matching preview and must never reach the customer.

    Two ways in: a quoted Total must MATCH a preview this turn, and a bill-SHAPED
    message (two or more distinct component lines) requires a preview to have run at
    all — which is what catches a fabricated bill whose Total line the quoted-total
    parser alone would miss."""

    def _preview_step(self, total):
        return {"tool": "preview_bill", "args": "{}", "result": {"total": str(total)}}

    # --- bill-shape detection (Issue 3) ---------------------------------------

    def test_zero_tax_bill_block_is_caught_even_with_no_preview(self):
        """The exact Issue 3 first bill: shown BEFORE the payment method was chosen,
        so tax and delivery were filled in as Rs. 0. Nothing produced it."""
        premature = (
            "Aapka order:\n1x Chicken Tikka Pizza\n"
            "Subtotal Rs. 1150\nTax: Rs. 0\nDelivery: Rs. 0\nTotal: Rs. 1150"
        )
        assert grounding.looks_like_a_bill(premature) is True
        assert _bill_unbacked(premature, trace=[]) is True

    def test_a_lone_total_is_not_bill_shaped(self):
        """The false-positive control. A budget estimate ("total Rs. 580") and an
        order-status reply quote a total with no preview behind them and must keep
        their existing behaviour — only the quoted-total rule may apply to them, not
        the new bill-shape rule."""
        assert grounding.looks_like_a_bill("Ye sab milakar total Rs. 580 ka hai.") is False
        assert grounding.looks_like_a_bill("Order AB-1234 ka total Rs. 1422.50 hai.") is False

    def test_two_distinct_components_are_bill_shaped(self):
        assert grounding.looks_like_a_bill(
            "Subtotal: Rs. 1150\nDelivery: Rs. 100"
        ) is True

    def test_subtotal_alone_does_not_count_as_a_total_label(self):
        """'total' hides inside 'Subtotal' — without the word boundary a single
        Subtotal line would look like two components and trip the guard."""
        assert grounding.looks_like_a_bill("Subtotal: Rs. 1150") is False

    def test_bill_shaped_reply_backed_by_a_preview_passes(self):
        """A real read-back is bill-shaped by design; a preview this turn clears it."""
        readback = (
            "Subtotal: Rs. 1150\nTax: Rs. 92.00\nDelivery: Rs. 100.00\n"
            "Total: Rs. 1342.00"
        )
        assert _bill_unbacked(
            readback, [self._preview_step("1342.00")]
        ) is False

    def test_bill_without_a_parseable_total_still_needs_a_preview(self):
        """Bill-shaped but no Total line to compare — the preview requirement is what
        catches it, since there is no number to match."""
        partial = "Subtotal: Rs. 1150\nDelivery: Rs. 100"
        assert agent._quoted_total(partial) is None
        assert _bill_unbacked(partial, trace=[]) is True
        assert _bill_unbacked(
            partial, [self._preview_step("1342.00")]
        ) is False

    def test_flags_a_total_with_no_preview_this_turn(self):
        readback = "Aapka order:\nTotal: Rs. 1150\nConfirm karein?"
        assert _bill_unbacked(readback, trace=[]) is True

    def test_flags_a_fabricated_total_even_above_subtotal(self):
        """The AB-F6DF70 case: Tax Rs. 284 (a made-up 10%) + Delivery Rs. 150, Total
        Rs. 3274 — ABOVE the Rs. 2840 subtotal, so the old magnitude rule missed it.
        With no preview backing it, it is caught."""
        readback = (
            "Subtotal: Rs. 2840\nTax: Rs. 284\nDelivery: Rs. 150\n"
            "Total: Rs. 3274\nConfirm karein?"
        )
        assert _bill_unbacked(readback, trace=[]) is True

    def test_passes_a_total_backed_by_a_preview_this_turn(self):
        readback = (
            "Subtotal: Rs. 2840\nTax: Rs. 426\nDelivery: Rs. 80\n"
            "Total: Rs. 3346.00\nConfirm karein?"
        )
        trace = [self._preview_step("3346.00")]
        assert _bill_unbacked(readback, trace) is False

    def test_ignores_replies_that_quote_no_total(self):
        assert _bill_unbacked("Aap ka naam kya hai?", trace=[]) is False

    def test_quoted_total_binds_the_total_line_not_the_subtotal_line(self):
        """A correct read-back lists both Subtotal and Total. The parser must bind
        the Total, never the (lower) Subtotal — 'total' hides inside 'Sub-total', so
        without a leading word boundary every proper read-back would be mis-read."""
        readback = (
            "Subtotal: Rs. 1150\nTax: Rs. 172.50\nDelivery: Rs. 100.00\n"
            "Total: Rs. 1422.50\nConfirm karein?"
        )
        assert agent._quoted_total(readback) == Decimal("1422.50")

    def test_unbacked_readback_is_suppressed_and_forces_preview(
        self, db, conversation, pizza, menu_item, scripted_model,
    ):
        """End to end: round 1 the model reads back a self-computed total (no
        preview_bill this turn); the guard suppresses it and forces preview_bill;
        round 3 it reads back the real total. The customer only ever sees the
        corrected reply."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)
        db.flush()
        real = billing.compute_bill(
            subtotal=_subtotal(conversation), delivery_fee=pizza.delivery_fee,
            discount=Decimal("0.00"), method=PaymentMethod.COD,
        ).total

        fabricated = "Aapka order:\n2x Chicken Tikka Pizza\nTotal: Rs. 2999\nConfirm karein?"
        corrected = f"Aapka order taiyar hai.\nTotal: Rs. {real}\nConfirm karein?"
        scripted_model(
            [
                completion(message(content=fabricated)),
                completion(
                    message(tool_calls=[tool_call("p", "preview_bill", {"payment_method": "cod"})])
                ),
                completion(message(content=corrected)),
            ]
        )

        reply, trace = agent.generate_reply(db, conversation)

        assert reply == corrected, "the self-computed total must not be what's sent"
        assert agent._quoted_total(reply) == real
        assert any(t["tool"] == "preview_bill" for t in trace), (
            "the guard must have forced a preview_bill call"
        )

    def test_replays_the_rs_1150_transcript_subtotal_shown_as_total(
        self, db, conversation, pizza, menu_item, scripted_model,
    ):
        """The real WhatsApp transcript, verbatim. One Chicken Tikka Pizza (Rs. 1150).
        The model read back "Total: Rs. 1150" — the bare food subtotal. The true COD
        bill is 1150 + 15% (172.50) + 100 delivery = Rs. 1422.50. The guard must catch
        the Rs. 1150 read-back, force preview_bill, and the customer only ever sees
        Rs. 1422.50."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        db.flush()
        assert _subtotal(conversation) == Decimal("1150.00"), "one pizza = Rs. 1150"

        bill = billing.compute_bill(
            subtotal=Decimal("1150.00"), delivery_fee=pizza.delivery_fee,
            discount=Decimal("0.00"), method=PaymentMethod.COD,
        )
        assert bill.total == Decimal("1422.50")

        understated = "Aapka order:\n1x Chicken Tikka Pizza\nTotal: Rs. 1150\nConfirm karein?"
        corrected = (
            "Aapka order:\n1x Chicken Tikka Pizza\n"
            f"Subtotal: Rs. 1150\nTax: Rs. {bill.tax_amount}\n"
            f"Delivery: Rs. {bill.delivery_fee}\nTotal: Rs. {bill.total}\nConfirm karein?"
        )
        # Sanity: the bare Rs. 1150, with no preview this turn, is unbacked.
        assert _bill_unbacked(understated, trace=[]) is True

        scripted_model(
            [
                completion(message(content=understated)),
                completion(
                    message(tool_calls=[tool_call("p", "preview_bill", {"payment_method": "cod"})])
                ),
                completion(message(content=corrected)),
            ]
        )

        reply, trace = agent.generate_reply(db, conversation)

        assert reply == corrected
        assert "Rs. 1150\nConfirm" not in reply, "the customer must never confirm the bare subtotal"
        assert agent._quoted_total(reply) == Decimal("1422.50")
        assert any(t["tool"] == "preview_bill" for t in trace)


class TestSwitchToOnlineAfterCodGuard:
    """Bug 2: after a COD order is placed the customer asks to pay online, and the
    model IGNORES it — no lie (so the fake-link guard stays silent), no answer. The
    request must not vanish; the customer gets the committed-to-COD / place-a-new-
    online-order reply deterministically."""

    def test_detector_fires_on_an_ignored_post_cod_online_request(
        self, db, cod_order, conversation,
    ):
        # cod_order is the customer's most recent order, COD + PENDING, cart empty.
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment kar sakta hoon?", trace=[]
        ) is True

    def test_detector_ignores_a_message_that_never_mentions_online(
        self, db, cod_order, conversation,
    ):
        assert agent._switch_to_online_after_cod(
            db, conversation, "mera order kahan hai?", trace=[]
        ) is False

    def test_detector_does_not_fire_while_a_genuinely_new_order_is_being_built(
        self, db, cod_order, conversation,
    ):
        """A non-empty cart whose items DON'T match the placed order is a real new
        order — 'online' is its payment-method choice, not a switch. Must not fire."""
        conversation.cart = {
            "items": [{
                "menu_item_id": 1, "restaurant_id": 1,
                "name": "x", "price": "100.00", "quantity": 1, "notes": None,
            }]
        }
        assert agent._switch_to_online_after_cod(
            db, conversation, "online se pay karunga", trace=[]
        ) is False

    def test_detector_fires_when_cart_is_a_rebuild_of_the_placed_order(
        self, db, cod_order, conversation, menu_item,
    ):
        """AB-F6DF70 issue 5: on the online-payment request the model RE-ADDED the
        just-placed order's items, so the cart is non-empty but is a rebuild. The old
        cart-empty gate was defeated; matching the cart against the order catches it."""
        # cod_order == cart_with_pizza == 2x this menu_item. Rebuild the same cart.
        conversation.cart = {
            "items": [{
                "menu_item_id": menu_item.id, "restaurant_id": menu_item.restaurant_id,
                "name": menu_item.name, "price": "100.00", "quantity": 2, "notes": None,
            }]
        }
        assert agent._switch_to_online_after_cod(
            db, conversation, "Online payment ho sakti h Kia?", trace=[]
        ) is True

    def test_detector_does_not_fire_when_this_turn_placed_an_order(
        self, db, cod_order, conversation,
    ):
        """A correctly-built new online order this turn (with its own real link)
        must pass through untouched, not be overridden by the guard."""
        trace = [{
            "tool": "place_order",
            "args": "{}",
            "result": {"order_number": "AB-NEW999", "payment_link": "https://pay.test/x"},
        }]
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment", trace=trace
        ) is False

    def test_detector_does_not_fire_without_a_prior_order(
        self, db, conversation,
    ):
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment karni hai", trace=[]
        ) is False

    def test_detector_does_not_fire_when_latest_order_is_already_online(
        self, db, cod_order, conversation,
    ):
        cod_order.payment_method = PaymentMethod.JAZZCASH
        db.flush()
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment", trace=[]
        ) is False

    def test_detector_does_not_fire_when_the_cod_order_was_cancelled(
        self, db, cod_order, conversation,
    ):
        cod_order.status = OrderStatus.CANCELLED
        db.flush()
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment", trace=[]
        ) is False

    def test_detector_does_not_fire_when_the_cod_order_was_delivered(
        self, db, cod_order, conversation,
    ):
        """A delivered COD order is done — payment happened at the door; there is
        nothing to switch."""
        cod_order.status = OrderStatus.DELIVERED
        db.flush()
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment", trace=[]
        ) is False

    def test_detector_ignores_an_order_from_a_PREVIOUS_conversation(
        self, db, cod_order, conversation,
    ):
        """THE CONVERSATION 723 CASE. The customer placed a COD order days ago in
        another conversation, then started a fresh one and said "... aur online".
        They were told "aapka order pehle hi Cash on Delivery par place ho chuka
        hai" about an order they had not mentioned and could not see.

        Simulated the way it really happened: the order predates this
        conversation's start.
        """
        from datetime import timedelta

        conversation.created_at = agent._as_utc(cod_order.created_at) + timedelta(minutes=5)
        db.flush()

        assert agent._switch_to_online_after_cod(
            db, conversation, "Address hai jubilee market aur online", trace=[]
        ) is False

    def test_detector_ignores_an_order_older_than_the_recency_window(
        self, db, cod_order, conversation,
    ):
        """The second bound, independent of conversation scoping: a conversation
        kept alive for days by messages inside the idle window must not resurrect
        an order from its first day.

        AB-5ABBE2 sat PENDING for five days and stayed "live" the whole time —
        restaurants rarely accept or cancel, so the status check bounds nothing.
        """
        from datetime import timedelta

        stale = datetime.now(timezone.utc) - convo.CONVERSATION_IDLE_TIMEOUT - timedelta(hours=1)
        cod_order.created_at = stale
        conversation.created_at = stale - timedelta(minutes=5)  # same conversation
        db.flush()

        assert cod_order.status not in (OrderStatus.CANCELLED, OrderStatus.DELIVERED)
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment karni hai", trace=[]
        ) is False

    def test_detector_still_fires_for_an_order_placed_in_this_conversation(
        self, db, cod_order, conversation,
    ):
        """The Bug 2 fix must survive the narrowing: an order placed moments ago
        in THIS conversation still gets the deterministic steer."""
        assert agent._as_utc(cod_order.created_at) >= agent._as_utc(conversation.created_at)
        assert agent._switch_to_online_after_cod(
            db, conversation, "online payment kar sakta hoon?", trace=[]
        ) is True

    def test_ignored_switch_reply_is_replaced_end_to_end(
        self, db, cod_order, conversation, scripted_model, monkeypatch,
    ):
        """Full path through handle_incoming_message: the model produces a reply that
        ignores the online-payment request (no lie, no link) and the customer instead
        receives the committed-to-COD offer to place a new online order."""
        scripted_model(
            [completion(message(content="Aapka order jald hi deliver ho jayega."))] * 4
        )
        sent = []
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))

        agent.handle_incoming_message(db, conversation, "online payment kar sakta hoon?")

        assert len(sent) == 1
        assert sent[0] == agent.FAKE_LINK_REPLACEMENT
        assert "jald hi deliver" not in sent[0], "the ignoring reply must be suppressed"


class TestDiscoveryHonestyGuard:
    """Bug 3: the model must not contradict a genuine zero-match by naming
    restaurants anyway, nor present a description-only weak match as a confirmed
    dish. Both are force-regenerated once; worst case is extra latency, never a
    wrong message to the customer."""

    ZERO_MATCH_TRACE = [{
        "tool": "find_restaurants",
        "args": '{"query": "burger"}',
        "result": {"query": "burger", "restaurants": [], "found_anywhere": False},
    }]

    def _weak_trace(self, name):
        return [{
            "tool": "find_restaurants",
            "args": '{"query": "spicy"}',
            "result": {
                "query": "spicy",
                "restaurants": [{"id": 1, "name": name, "match_strength": "weak"}],
                "weak_matches_only": True,
            },
        }]

    # --- Part A: contradiction ------------------------------------------------

    def test_contradiction_fires_when_zero_match_reply_names_a_restaurant(
        self, db, conversation,
    ):
        reply = "Sorry, burger nahi hai.\n1. Karachi Biryani House\n2. Pizza Junction"
        assert agent._discovery_contradiction(
            db, conversation, reply, self.ZERO_MATCH_TRACE
        ) is True

    def test_contradiction_not_flagged_when_reply_offers_only_cuisines(
        self, db, conversation,
    ):
        reply = (
            "Burger available nahi hai. Humare paas Desi, Pizza aur Chinese hai — "
            "kaunsi try karein?"
        )
        assert agent._discovery_contradiction(
            db, conversation, reply, self.ZERO_MATCH_TRACE
        ) is False

    def test_contradiction_not_flagged_while_a_restaurant_is_active(
        self, db, conversation, pizza,
    ):
        """With an active restaurant, 'Pizza Junction mein burger nahi hai' is the
        correct answer, not a contradiction."""
        conversation.active_restaurant_id = pizza.id
        reply = "Pizza Junction mein burger nahi hai. Kuch aur try karein?"
        assert agent._discovery_contradiction(
            db, conversation, reply, self.ZERO_MATCH_TRACE
        ) is False

    def test_contradiction_not_flagged_without_a_zero_match(self, db, conversation):
        reply = "1. Karachi Biryani House\n2. Pizza Junction"
        trace = [{
            "tool": "find_restaurants", "args": "{}",
            "result": {"restaurants": [{"id": 1, "name": "Karachi Biryani House"}]},
        }]
        assert agent._discovery_contradiction(db, conversation, reply, trace) is False

    # --- Part B: weak-match overclaim ----------------------------------------

    def test_overclaim_fires_on_confident_weak_match_without_verification(
        self, conversation,
    ):
        reply = "Haan, Karachi Biryani House mein spicy dish hai. Order karun?"
        assert agent._discovery_overclaim(
            conversation, reply, self._weak_trace("Karachi Biryani House")
        ) is True

    def test_overclaim_not_flagged_when_reply_hedges(self, conversation):
        reply = "Karachi Biryani House mein ho sakta hai — menu dekh ke confirm karta hoon."
        assert agent._discovery_overclaim(
            conversation, reply, self._weak_trace("Karachi Biryani House")
        ) is False

    def test_overclaim_not_flagged_when_get_menu_was_called(self, conversation):
        reply = "Karachi Biryani House mein spicy dish hai."
        trace = self._weak_trace("Karachi Biryani House") + [{
            "tool": "get_menu", "args": "{}",
            "result": {"restaurant": {"id": 1, "name": "Karachi Biryani House"}},
        }]
        assert agent._discovery_overclaim(conversation, reply, trace) is False

    def test_overclaim_not_flagged_when_reply_does_not_name_the_restaurant(
        self, conversation,
    ):
        reply = "Aap kya order karna chahenge?"
        assert agent._discovery_overclaim(
            conversation, reply, self._weak_trace("Karachi Biryani House")
        ) is False

    def test_overclaim_not_flagged_on_a_strong_match(self, conversation):
        reply = "Karachi Biryani House mein biryani hai."
        trace = [{
            "tool": "find_restaurants", "args": "{}",
            "result": {"restaurants": [
                {"id": 1, "name": "Karachi Biryani House", "match_strength": "strong"}
            ]},
        }]
        assert agent._discovery_overclaim(conversation, reply, trace) is False

    # --- End to end through generate_reply -----------------------------------

    def test_contradiction_reply_is_regenerated(self, db, conversation, scripted_model):
        """burger is a genuine zero-match; the model first lists restaurants anyway,
        the guard regenerates, and the customer sees the cuisines-only answer."""
        bad = "Burger nahi hai.\n1. Karachi Biryani House\n2. Pizza Junction"
        good = (
            "Burger available nahi hai. Humare paas Desi, Pizza aur Chinese hai — "
            "kaunsi try karein?"
        )
        scripted_model([
            completion(message(tool_calls=[tool_call("f", "find_restaurants", {"query": "burger"})])),
            completion(message(content=bad)),
            completion(message(content=good)),
        ])

        reply, trace = agent.generate_reply(db, conversation)

        assert reply == good
        assert any(t["tool"] == "find_restaurants" for t in trace)

    def test_overclaim_reply_is_regenerated(self, db, conversation, scripted_model):
        """spicy is a description-only (weak) match; the model first claims the dish
        is there, the guard regenerates, and the customer gets a hedged answer."""
        bad = "Haan, Karachi Biryani House mein spicy dish hai. Order karun?"
        good = "Karachi Biryani House ho sakta hai — menu dekh ke confirm karta hoon."
        scripted_model([
            completion(message(tool_calls=[tool_call("f", "find_restaurants", {"query": "spicy"})])),
            completion(message(content=bad)),
            completion(message(content=good)),
        ])

        reply, _trace = agent.generate_reply(db, conversation)

        assert reply == good


class TestDiscoveryPaddingGuard:
    """Issue 2: a GOOD (non-empty, strong) discovery result padded with a restaurant
    the search never returned. "Biryani hai?" returns only Karachi Biryani House —
    Pizza Junction matches 'biryani' on no field and no menu item — but the reply
    offered both, and Pizza Junction has no biryani.

    The zero-match and weak-match cases belong to the two guards above; this one
    covers only the non-empty result, so the three partition the space."""

    def _found(self, *names):
        return [{
            "tool": "find_restaurants",
            "args": '{"query": "biryani"}',
            "result": {
                "query": "biryani",
                "restaurants": [
                    {"id": i, "name": n, "match_strength": "strong"}
                    for i, n in enumerate(names, start=1)
                ],
            },
        }]

    def test_fires_when_reply_adds_a_restaurant_the_search_did_not_return(
        self, db, conversation,
    ):
        """The real Issue 2 transcript."""
        reply = (
            "Biryani serve karne wale restaurants:\n"
            "1. Karachi Biryani House\n2. Pizza Junction\n\nKaunsa chahenge?"
        )
        assert _kind(
            db, conversation, reply, self._found("Karachi Biryani House")
        ) is True

    def test_not_flagged_when_reply_names_only_what_was_returned(
        self, db, conversation,
    ):
        reply = "Biryani ke liye Karachi Biryani House hai. Menu dikhaun?"
        assert _kind(
            db, conversation, reply, self._found("Karachi Biryani House")
        ) is False

    def test_not_flagged_when_the_extra_name_is_the_active_restaurant(
        self, db, conversation, pizza,
    ):
        """With Pizza Junction active, contrasting it with the search result is the
        honest answer, not padding — the same carve-out the contradiction guard makes."""
        conversation.active_restaurant_id = pizza.id
        reply = "Pizza Junction mein biryani nahi hai, lekin Karachi Biryani House mein hai."
        assert _kind(
            db, conversation, reply, self._found("Karachi Biryani House")
        ) is False

    def test_not_flagged_when_get_menu_was_called_for_the_extra_restaurant(
        self, db, conversation, pizza,
    ):
        """Pulling the menu earns the right to talk about that restaurant."""
        trace = self._found("Karachi Biryani House") + [{
            "tool": "get_menu", "args": "{}",
            "result": {"restaurant": {"id": pizza.id, "name": "Pizza Junction"}},
        }]
        reply = "Karachi Biryani House mein biryani hai; Pizza Junction mein nahi."
        assert _kind(db, conversation, reply, trace) is False

    def test_not_flagged_on_a_zero_match(self, db, conversation):
        """Delegated to _discovery_contradiction — this guard must not double-fire."""
        trace = [{
            "tool": "find_restaurants", "args": '{"query": "burger"}',
            "result": {"query": "burger", "restaurants": [], "found_anywhere": False},
        }]
        reply = "Burger nahi hai.\n1. Karachi Biryani House"
        assert _kind(db, conversation, reply, trace) is False

    def test_not_flagged_when_no_discovery_ran_this_turn(self, db, conversation):
        reply = "Karachi Biryani House aur Pizza Junction dono achay hain."
        assert _kind(db, conversation, reply, []) is False

    def test_selection_path_result_allows_the_selected_restaurant(
        self, db, conversation,
    ):
        """find_restaurants' selection path returns a single `restaurant` key and no
        `restaurants` list — naming that restaurant must not count as padding."""
        trace = [{
            "tool": "find_restaurants", "args": '{"query": "Pizza Junction"}',
            "result": {
                "query": "Pizza Junction",
                "selected_from_shown_list": True,
                "restaurant": {"id": 1, "name": "Pizza Junction"},
                "items": [],
            },
        }]
        reply = "Pizza Junction ka menu ye raha."
        assert _kind(db, conversation, reply, trace) is False

    def test_allowed_name_is_masked_before_scanning_for_absent_ones(
        self, db, conversation, biryani,
    ):
        """Substring trap: an allowed longer name must not leave a shorter absent
        name matching inside it. Renaming the seeded restaurant makes the returned
        name a strict superstring of the one that is NOT in the result."""
        biryani.name = "Karachi Biryani House Deluxe"
        db.flush()
        reply = "Sirf Karachi Biryani House Deluxe mein biryani hai."
        assert _kind(
            db, conversation, reply, self._found("Karachi Biryani House Deluxe")
        ) is False

    def test_padded_reply_is_regenerated_end_to_end(
        self, db, conversation, scripted_model,
    ):
        """Through generate_reply: the padded draft never reaches the customer."""
        bad = (
            "Biryani serve karne wale restaurants:\n"
            "1. Karachi Biryani House\n2. Pizza Junction\n\nKaunsa chahenge?"
        )
        good = "Biryani ke liye Karachi Biryani House hai. Menu dikhaun?"
        scripted_model([
            completion(message(tool_calls=[
                tool_call("f", "find_restaurants", {"query": "biryani"}),
            ])),
            completion(message(content=bad)),
            completion(message(content=good)),
        ])

        reply, _trace = agent.generate_reply(db, conversation)

        assert reply == good
        assert "Pizza Junction" not in reply


class TestUngroundedRestaurantListGuard:
    """Issue #1: a numbered restaurant list that NO listing tool produced this turn.

    The gap the other three discovery guards cannot see. Each of them reads a
    find_restaurants result out of the trace, so a turn with NO tool call at all
    returns False on their first line — and that is the most dangerous turn, because
    then the entire claim is invented.

    Production (conv 715): asked "Haleem hai?" the model called nothing, reused the
    four restaurants from its own greeting, and replied "Here are restaurants serving
    haleem: 1. Karachi Biryani House 2. Mandi House". Nothing in the catalogue
    matches 'haleem' at all.
    """

    FABRICATED = (
        "Haleem ki availability check karta hun.\n\n"
        "Here are restaurants serving haleem:\n"
        "1. Karachi Biryani House\n2. Pizza Junction\n\n"
        "Aap kis restaurant se order karna chahenge?"
    )

    def _listing(self, tool="find_restaurants"):
        return [{"tool": tool, "args": "{}", "result": {"restaurants": [
            {"id": 1, "name": "Karachi Biryani House"},
            {"id": 2, "name": "Pizza Junction"},
        ]}}]

    def test_fires_on_a_list_with_no_tool_call_at_all(self, db, conversation):
        """The exact production failure."""
        assert _list_kind(db, conversation, self.FABRICATED, []) is True

    @pytest.mark.parametrize(
        "tool", ["list_restaurants", "find_restaurants", "search_restaurants_by_item"]
    )
    def test_not_flagged_when_a_listing_tool_ran(self, db, conversation, tool):
        """A real listing happened — the CONTENT guards take over from here."""
        assert _list_kind(
            db, conversation, self.FABRICATED, self._listing(tool)
        ) is False

    def test_fires_when_only_get_menu_ran(self, db, conversation):
        """conv 715's next turn: get_menu failed on the empty restaurant, and the
        model reported a 3-restaurant list that also came from memory."""
        trace = [{"tool": "get_menu", "args": "{}",
                  "result": {"error": "Mandi House has no items available right now."}}]
        reply = (
            "Mandi House mein items available nahi hain.\n\n"
            "Available restaurants:\n1. Karachi Biryani House\n2. Wok & Roll\n"
            "3. Pizza Junction"
        )
        assert _list_kind(db, conversation, reply, trace) is True

    def test_a_single_numbered_restaurant_is_not_a_list(self, db, conversation):
        """Threshold is two: one numbered line naming a restaurant is usually a
        confirmation, not an offer of choices. Precision over recall, deliberately."""
        reply = "Aapka order confirm hai:\n1. Pizza Junction — Chicken Tikka Pizza"
        assert _list_kind(db, conversation, reply, []) is False

    def test_numbered_menu_items_are_not_a_restaurant_list(self, db, conversation):
        """A numbered list of FOOD must not trip the guard."""
        reply = (
            "Yeh items available hain:\n"
            "1. Chicken Biryani — Rs. 450\n2. Beef Biryani — Rs. 550\n"
            "3. Seekh Kebab — Rs. 520"
        )
        assert _list_kind(db, conversation, reply, []) is False

    def test_past_order_enumeration_is_allowed(self, db, conversation):
        """Order-status talk legitimately names restaurants with no listing tool —
        suppressing it would break a normal, truthful reply."""
        reply = (
            "Aapke recent orders:\n"
            "1. AB-2992A8 — Wok & Roll — Rs. 962.40\n"
            "2. AB-909804 — Karachi Biryani House — Rs. 2564.00"
        )
        assert _list_kind(db, conversation, reply, []) is False

    def test_get_order_status_this_turn_is_allowed(self, db, conversation):
        trace = [{"tool": "get_order_status", "args": "{}", "result": {"order_number": "AB-2992A8"}}]
        reply = "Aapke orders:\n1. Karachi Biryani House\n2. Pizza Junction"
        assert _list_kind(db, conversation, reply, trace) is False

    def test_empty_reply_is_not_flagged(self, db, conversation):
        assert _list_kind(db, conversation, None, []) is False
        assert _list_kind(db, conversation, "", []) is False

    def test_prose_without_a_list_is_the_known_residual_gap(self, db, conversation):
        """Documents an accepted limitation rather than a bug: a one-sentence
        fabrication carries no numbered list, so this guard does not see it.
        Catching it would need affirmation detection, which false-positives on
        legitimate lines like 'Haan, aapka order Pizza Junction se aa raha hai'."""
        reply = "Haan, Karachi Biryani House mein haleem hai."
        assert _list_kind(db, conversation, reply, []) is False

    def test_fabricated_list_is_suppressed_and_a_search_is_forced(
        self, db, conversation, scripted_model,
    ):
        """End to end: the invented list never reaches the customer, the guard forces
        a real search, and the honest zero-match answer is what gets sent."""
        honest = (
            "Haleem available nahi hai. Humare paas Desi, Pizza aur Chinese hai — "
            "kaunsi try karein?"
        )
        scripted_model([
            completion(message(content=self.FABRICATED)),
            completion(message(tool_calls=[
                tool_call("f", "find_restaurants", {"query": "haleem"}),
            ])),
            completion(message(content=honest)),
        ])

        reply, trace = agent.generate_reply(db, conversation)

        assert reply == honest
        assert any(t["tool"] == "find_restaurants" for t in trace), "a search must be forced"
        for name in ("Karachi Biryani House", "Pizza Junction", "Wok & Roll"):
            assert name not in reply


class TestRomanUrduStallPatterns:
    """The stall detector forces a tool call when the model says it will "check" and
    then calls nothing. The list was English-only, so the bot could stall in the
    customer's own language and slip past — which is how conv 715 began."""

    def test_the_real_production_stall_is_detected(self):
        assert agent._is_stall("Haleem ki availability check karta hun.") is True

    @pytest.mark.parametrize("phrase", [
        "Main check karta hun",
        "Abhi check karti hun",
        "Menu dekh kar batata hun",
        "Pata kar ke batata hun",
        "Ek minute, dekhta hun",
        "Thori dair mein batata hun",
    ])
    def test_roman_urdu_stalls_are_detected(self, phrase):
        assert agent._is_stall(phrase) is True

    def test_english_stalls_still_work(self):
        assert agent._is_stall("Let me check the menu") is True
        assert agent._is_stall("One moment please") is True

    def test_ordinary_replies_are_not_stalls(self):
        """The list must not swallow normal conversation."""
        for ordinary in (
            "Aapka order Pizza Junction se aayega.",
            "Yeh items available hain: Chicken Biryani — Rs. 450",
            "Aapka naam kya hai?",
            "Order place ho gaya hai.",
        ):
            assert agent._is_stall(ordinary) is False


# --------------------------------------------------------------------------- #
# Salvage never invents (conversation 723)
# --------------------------------------------------------------------------- #


def _bad_request(message="tool_use_failed: could not parse tool call"):
    """The 400 Groq returns when the model emits a syntactically broken tool call."""
    import httpx
    from groq import BadRequestError

    return BadRequestError(
        message,
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.groq.test")),
        body=None,
    )


@pytest.fixture
def scripted_model_raising(monkeypatch):
    """Like `scripted_model`, but any Exception in the sequence is RAISED rather
    than returned — needed to drive the malformed-tool-call path."""

    def install(responses):
        stream = iter(responses)
        calls = {"count": 0}

        class Completions:
            def create(self, **kwargs):
                calls["count"] += 1
                item = next(stream)
                if isinstance(item, Exception):
                    raise item
                return item

        class Client:
            chat = types.SimpleNamespace(completions=Completions())

        monkeypatch.setattr(agent, "_client", lambda: Client())
        return calls

    return install


class TestSalvageNeverInvents:
    """`generate_reply` has two escape hatches that return WITHOUT running the
    grounding, discovery or language guards: a malformed tool call Groq rejects,
    and the round budget running out. Both call `_force_text_reply`, which asks the
    model to answer "using only the tool results above" with tools switched off.

    That is correct when tools ran — place_order may have succeeded, and dropping
    the turn hid a real order from a customer who then re-ordered it. It is
    catastrophic when NOTHING ran: there are no results above, no guard downstream,
    and the model fills the vacuum. Conversation 723 [1027], a real customer:

        "Qorma serve karne wale restaurants:
         1. Karachi Biryani House
         2. Mandi House"

    No tool returned that list (the row's `meta` is NULL — the trace was empty),
    Mandi House had never been shown to that customer, and Qorma exists nowhere in
    the catalogue. grounding.audit flags it `unlisted_offer` on replay; it was
    never asked, because this path skips the audit entirely.
    """

    def test_no_tool_ran_so_no_reply_is_invented(
        self, db, conversation, scripted_model_raising,
    ):
        """THE CONVERSATION 723 CASE. Every attempt is malformed, nothing executes,
        retries run out. The customer must get the honest fallback — never prose
        the model composed out of nothing."""
        calls = scripted_model_raising([
            _bad_request(),   # attempt 1 -> retry
            _bad_request(),   # attempt 2 -> retry
            _bad_request(),   # attempt 3 -> retries exhausted
        ])

        reply, trace = agent.generate_reply(db, conversation)

        assert trace == []
        assert reply == agent.FALLBACK_REPLY
        # Exactly 3 calls: the salvage completion was never even attempted. If a 4th
        # had been made, the model would have been invited to invent from nothing.
        assert calls["count"] == 3

    def test_salvage_still_reports_a_turn_where_tools_DID_run(
        self, db, conversation, scripted_model_raising,
    ):
        """The reason this path exists must survive the fix: once a tool has run,
        a later malformed call still salvages the turn rather than losing it."""
        calls = scripted_model_raising([
            completion(message(tool_calls=[tool_call("c1", "list_restaurants", {})])),
            _bad_request(),                                   # now trace is non-empty
            completion(message(content="Yeh restaurants available hain: ...")),
        ])

        reply, trace = agent.generate_reply(db, conversation)

        assert trace, "a tool ran; the trace must not be empty"
        assert reply == "Yeh restaurants available hain: ..."
        assert reply != agent.FALLBACK_REPLY
        assert calls["count"] == 3  # the salvage completion WAS made

    def test_salvage_helper_refuses_to_call_the_model_on_an_empty_trace(
        self, db, conversation,
    ):
        """Unit-level invariant, covering BOTH call sites at once: with nothing to
        report, `_salvage_reply` must not reach the model at all."""

        class ExplodingClient:
            class _Completions:
                def create(self, **kwargs):
                    raise AssertionError(
                        "the model must not be asked to summarise an empty trace"
                    )

            chat = types.SimpleNamespace(completions=_Completions())

        reply = agent._salvage_reply(db, ExplodingClient(), [], [], conversation)
        assert reply == agent.FALLBACK_REPLY

    def test_salvage_helper_does_summarise_a_real_trace(self, db, conversation, monkeypatch):
        """The other half of the invariant — a non-empty trace is still salvaged."""
        monkeypatch.setattr(
            agent,
            "_force_text_reply",
            lambda client, messages, extra_nudge=None: "Aapka order AB-123456 place ho gaya.",
        )
        trace = [{"tool": "place_order", "result": {"order_number": "AB-123456"}}]

        reply = agent._salvage_reply(db, object(), [], trace, conversation)

        assert reply == "Aapka order AB-123456 place ho gaya."


class TestSalvagedReplyIsAudited:
    """1.2 — the salvaged reply itself goes through the content checks.

    Closing the empty-trace hole (1.1) stopped the model inventing from NOTHING.
    It can still invent on top of a real trace: `_force_text_reply` runs with tools
    off and, before this, its output went straight to the customer with no audit.
    """

    def _texts(self, monkeypatch, *replies):
        """Script successive `_force_text_reply` returns and record the nudges."""
        stream = iter(replies)
        seen = []

        def fake(client, messages, extra_nudge=None):
            seen.append(extra_nudge)
            return next(stream)

        monkeypatch.setattr(agent, "_force_text_reply", fake)
        return seen

    # A trace that legitimately grounds ONE restaurant, so a reply naming a
    # DIFFERENT one is unambiguously fabricated.
    TRACE = [{
        "tool": "find_restaurants",
        "result": {"restaurants": [{"id": 2, "name": "Karachi Biryani House"}]},
    }]

    FABRICATED = (
        "Qorma serve karne wale restaurants:\n"
        "1. Karachi Biryani House\n"
        "2. Mandi House\n\n"
        "Aap kis restaurant se order karna chahenge?"
    )

    def test_clean_salvaged_reply_passes_straight_through(
        self, db, conversation, monkeypatch,
    ):
        nudges = self._texts(monkeypatch, "Karachi Biryani House se order kar sakte hain.")

        reply = agent._salvage_reply(db, object(), [], self.TRACE, conversation)

        assert reply == "Karachi Biryani House se order kar sakte hain."
        assert nudges == [None], "a clean reply must not trigger a corrective pass"

    def test_fabricated_salvaged_reply_is_corrected_once(
        self, db, conversation, monkeypatch,
    ):
        """The list names Mandi House, which no tool returned. One corrective pass,
        carrying the auditor's own nudge; the clean retry is what the customer gets."""
        nudges = self._texts(
            monkeypatch, self.FABRICATED, "Karachi Biryani House available hai.",
        )

        reply = agent._salvage_reply(db, object(), [], self.TRACE, conversation)

        assert reply == "Karachi Biryani House available hai."
        assert nudges[0] is None
        assert nudges[1] is not None, "the retry must carry the violation nudge"
        assert "search" in nudges[1].lower() or "list" in nudges[1].lower()

    def test_fabricating_twice_is_suppressed_not_sent(
        self, db, conversation, monkeypatch,
    ):
        """No order in the trace, so there is nothing truthful to report — the
        customer gets the honest fallback rather than the second fabrication."""
        self._texts(monkeypatch, self.FABRICATED, self.FABRICATED)

        reply = agent._salvage_reply(db, object(), [], self.TRACE, conversation)

        assert reply == agent.FALLBACK_REPLY
        assert "Mandi House" not in reply

    def test_fabricating_twice_still_reports_a_real_order(
        self, db, conversation, monkeypatch,
    ):
        """THE REGRESSION THIS PATH EXISTS TO PREVENT. Suppressing untrusted prose
        must never hide an order that was actually placed — that is the old
        'sorry, say that again' failure, where the customer re-ordered."""
        self._texts(monkeypatch, self.FABRICATED, self.FABRICATED)
        trace = self.TRACE + [{
            "tool": "place_order",
            "result": {"order_number": "AB-C5475E", "total": "980.00"},
        }]

        reply = agent._salvage_reply(db, object(), [], trace, conversation)

        assert "AB-C5475E" in reply
        assert "980.00" in reply
        assert "Mandi House" not in reply
        assert reply != agent.FALLBACK_REPLY


class TestRepeatGroundingViolationIsSuppressed:
    """1.4 — the main tool loop must not send a SECOND fabrication either.

    The grounding audit used to be gated on `grounding_corrected_once`, so after
    one correction it never ran again and the regenerated draft was delivered
    unaudited — "correct once, then send whatever comes back". A model that
    fabricated the same restaurant list twice had the second copy sent.

    This was also asymmetric with the salvage path, which since 1.2 suppresses a
    reply that fabricates twice. Same failure, two different outcomes depending on
    which code path the turn happened to take.

    One correction is still all that is spent; only the terminal action changed.
    """

    FABRICATED = (
        "Qorma serve karne wale restaurants:\n"
        "1. Karachi Biryani House\n"
        "2. Mandi House\n\n"
        "Aap kis restaurant se order karna chahenge?"
    )

    def _find_restaurants_call(self):
        return tool_call("c1", "find_restaurants", {"query": "qorma"})

    def test_corrected_reply_that_is_clean_is_sent_normally(
        self, db, conversation, scripted_model,
    ):
        """THE OVER-SUPPRESSION GUARD. One fabrication, then a clean reply — the
        customer must get the clean reply, exactly as before this change."""
        scripted_model([
            completion(message(tool_calls=[self._find_restaurants_call()])),
            completion(message(content=self.FABRICATED)),          # audited -> corrected
            completion(message(content="Qorma kisi bhi restaurant mein nahi hai.")),
        ])

        reply, _trace = agent.generate_reply(db, conversation)

        assert reply == "Qorma kisi bhi restaurant mein nahi hai."
        assert reply != agent.FALLBACK_REPLY

    def test_fabricating_twice_is_suppressed(self, db, conversation, scripted_model):
        """The second fabrication must never reach the customer."""
        scripted_model([
            completion(message(tool_calls=[self._find_restaurants_call()])),
            completion(message(content=self.FABRICATED)),   # corrected
            completion(message(content=self.FABRICATED)),   # repeat -> suppressed
        ])

        reply, _trace = agent.generate_reply(db, conversation)

        assert reply == agent.FALLBACK_REPLY
        assert "Mandi House" not in reply

    def test_fabricating_twice_still_reports_a_real_order(
        self, db, conversation, scripted_model, monkeypatch,
    ):
        """Suppression must not hide an order that was actually placed."""
        monkeypatch.setattr(
            agent,
            "_order_report",
            lambda trace: "Aapka order AB-C5475E place ho chuka hai. Total: Rs. 980.00.",
        )
        scripted_model([
            completion(message(tool_calls=[self._find_restaurants_call()])),
            completion(message(content=self.FABRICATED)),
            completion(message(content=self.FABRICATED)),
        ])

        reply, _trace = agent.generate_reply(db, conversation)

        assert "AB-C5475E" in reply
        assert "Mandi House" not in reply

    def test_the_audit_runs_on_the_second_draft_at_all(
        self, db, conversation, scripted_model, monkeypatch,
    ):
        """Pins the ungating itself. Before this change the audit was called at
        most ONCE per turn; the second draft was never examined."""
        seen = []
        real_audit = agent.grounding.audit

        def counting_audit(db_, conv, reply, trace, **kw):
            seen.append(reply)
            return real_audit(db_, conv, reply, trace, **kw)

        monkeypatch.setattr(agent.grounding, "audit", counting_audit)
        scripted_model([
            completion(message(tool_calls=[self._find_restaurants_call()])),
            completion(message(content=self.FABRICATED)),
            completion(message(content=self.FABRICATED)),
        ])

        agent.generate_reply(db, conversation)

        assert len(seen) >= 2, "the regenerated draft must be audited too"
        assert seen[0] == self.FABRICATED and seen[1] == self.FABRICATED


class TestOrderReport:
    """The deterministic reply, built from the trace rather than from model prose."""

    def test_reports_order_number_and_total(self):
        report = agent._order_report(
            [{"tool": "place_order", "result": {"order_number": "AB-123456", "total": "712.50"}}]
        )
        assert "AB-123456" in report
        assert "712.50" in report

    def test_includes_a_real_payment_link_verbatim(self):
        report = agent._order_report([{
            "tool": "place_order",
            "result": {
                "order_number": "AB-123456",
                "total": "712.50",
                "payment_link": "https://pay.example.test/abc123",
            },
        }])
        assert "https://pay.example.test/abc123" in report

    def test_none_when_no_order_was_placed(self):
        assert agent._order_report([{"tool": "get_menu", "result": {"items": []}}]) is None
        assert agent._order_report([]) is None

    def test_none_when_place_order_failed(self):
        """An error result carries no order_number — there is nothing to report."""
        assert agent._order_report(
            [{"tool": "place_order", "result": {"error": "missing_address"}}]
        ) is None

    def test_reports_a_duplicate_prevented_order(self):
        """duplicate_prevented still names a REAL order the customer has."""
        report = agent._order_report([{
            "tool": "place_order",
            "result": {"duplicate_prevented": True, "order_number": "AB-999999", "total": "500.00"},
        }])
        assert "AB-999999" in report
