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
        # Named "groq" for history; the LLM client now goes through the openai
        # package pointed at OpenRouter, and any provider raises OpenAIError on
        # transport failures. The behaviour asserted (fallback reply, no crash)
        # is provider-independent.
        from openai import OpenAIError

        # handle_incoming_message rolls back when Groq fails. Commit the conversation
        # first so the rollback doesn't discard the fixture's own rows — in production the
        # webhook has already committed the customer and the inbound message by this point.
        db.commit()

        class Exploding:
            def create(self, **kwargs):
                raise OpenAIError("service unavailable")

        class Client:
            chat = types.SimpleNamespace(completions=Exploding())

        monkeypatch.setattr(agent, "_client", lambda: Client())

        sent = []
        monkeypatch.setattr(agent, "send_text", lambda to, body: sent.append(body))

        agent.handle_incoming_message(db, conversation, "hi")

        assert sent == [agent.FALLBACK_REPLY], "the customer must not be left in silence"
