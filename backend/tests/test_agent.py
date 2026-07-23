"""End-to-end walk through the conversation flow: greeting (with inline restaurant
list) -> pick restaurant -> menu -> add to cart.

The Groq client is stubbed with a scripted sequence of responses per turn, exactly
like tests/test_agent_guards.py — deterministic, no network, no token cost. Each
call to `handle_incoming_message` is one customer message; the scripted responses
model what a well-behaved llama-3.3 reply looks like for that turn (a tool call
round followed by a text round, or just text when no tool is needed).
"""

import json
import types

import pytest
from sqlalchemy import select

from app.models import MenuItem
from app.services import agent


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
    """Install a fixed sequence of completions for the NEXT turn only, so each
    turn in the flow can be scripted independently."""

    def install(responses):
        stream = iter(responses)

        class Completions:
            def create(self, **kwargs):
                return next(stream)

        class Client:
            chat = types.SimpleNamespace(completions=Completions())

        monkeypatch.setattr(agent, "_client", lambda: Client())

    return install


@pytest.fixture
def sent(monkeypatch):
    captured: list[str] = []
    monkeypatch.setattr(agent, "send_text", lambda to, body: captured.append(body))
    return captured


class TestNewOrderingFlow:
    def test_greeting_to_add_to_cart(self, db, conversation, biryani, scripted_model, sent):
        chicken_biryani = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == biryani.id,
                MenuItem.name.ilike("%Chicken Biryani%"),
            )
        )

        # Turn 1: "hi" -> greeting turn now calls list_restaurants, replies with the
        # combined greeting + numbered list + "pick one" question in ONE message.
        scripted_model(
            [
                completion(
                    message(tool_calls=[tool_call("a", "list_restaurants", {})])
                ),
                completion(
                    message(
                        content=(
                            "Welcome to AbhiAya.\n\n"
                            "Available restaurants:\n"
                            "1. Karachi Biryani House\n"
                            "2. Pizza Junction\n"
                            "3. Wok & Roll\n\n"
                            "Which restaurant would you like to order from?"
                        )
                    )
                ),
            ]
        )
        agent.handle_incoming_message(db, conversation, "hi")

        greeting_reply = sent[-1]
        assert "Welcome to AbhiAya" in greeting_reply
        assert "Available restaurants:" in greeting_reply
        assert "1. " in greeting_reply, "restaurant list must be numbered"
        # Was: must end with the emoji-question. The turn SHAPE (greeting +
        # numbered list + forward-moving question) is the thing worth pinning;
        # the trailing 🍴 was tone, and the prompt no longer asks for emojis.
        assert greeting_reply.rstrip().endswith("?"), "must end with a question"

        # Turn 2: picks the restaurant by name -> get_menu, then items with prices.
        scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call(
                                "b",
                                "get_menu",
                                {"restaurant_name": "Karachi Biryani House"},
                            )
                        ]
                    )
                ),
                completion(
                    message(
                        content=(
                            "Karachi Biryani House ka menu:\n"
                            "Chicken Biryani — Rs. 450\n"
                            "Beef Biryani — Rs. 550\n"
                            "What would you like to order?"
                        )
                    )
                ),
            ]
        )
        agent.handle_incoming_message(db, conversation, "Karachi Biryani House")

        # Turn 3: "2 chicken biryani" -> add_to_cart, then asks for delivery address.
        scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call(
                                "c",
                                "add_to_cart",
                                {"menu_item_id": chicken_biryani.id, "quantity": 2},
                            )
                        ]
                    )
                ),
                completion(
                    message(
                        content=(
                            "2 Chicken Biryani added to your cart 🍔 What's the full "
                            "delivery address for this order?"
                        )
                    )
                ),
            ]
        )
        agent.handle_incoming_message(db, conversation, "2 chicken biryani")

        assert len(sent) == 3
        for reply in sent:
            assert not agent._leaks_tool_call(reply)

        quantities = [line["quantity"] for line in conversation.cart["items"]]
        assert quantities == [2], "the biryani was added exactly once"
        assert conversation.cart["items"][0]["menu_item_id"] == chicken_biryani.id
