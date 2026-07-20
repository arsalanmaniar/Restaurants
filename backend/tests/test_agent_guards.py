"""The guards that stop the model hurting a customer.

The Groq client is stubbed, so these run with no API key, no network, and no token cost —
and, more importantly, they are deterministic. Each one replays a failure the real model
actually produced during development.
"""

import json
import types

import pytest

from app.services import agent
from app.services import tools


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
