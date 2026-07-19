"""End-to-end walk through the new conversation flow: greeting -> delivery area ->
what they want -> restaurants serving it -> menu -> add to cart.

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

        # Turn 1: "hi" -> greeting, asks for delivery area + what they'd like. No tools.
        scripted_model(
            [
                completion(
                    message(
                        content=(
                            "Welcome to AbhiAya! 🍴 Which area should we deliver to, "
                            "and what would you like to eat today?"
                        )
                    )
                )
            ]
        )
        agent.handle_incoming_message(db, conversation, "hi")

        # Turn 2: gives the area -> AI asks what they want to eat. No tools.
        scripted_model(
            [completion(message(content="Karachi Saddar, noted 📍 What would you like to eat?"))]
        )
        agent.handle_incoming_message(db, conversation, "Karachi, Saddar")

        # Turn 3: "biryani" -> search_restaurants_by_item, then a numbered list.
        scripted_model(
            [
                completion(
                    message(
                        tool_calls=[
                            tool_call(
                                "a", "search_restaurants_by_item", {"query": "biryani"}
                            )
                        ]
                    )
                ),
                completion(
                    message(
                        content=(
                            "Yeh restaurants biryani serve karte hain:\n"
                            "1. Karachi Biryani House\n"
                            "\n"
                            "Konsa pasand karain gay? 🍴"
                        )
                    )
                ),
            ]
        )
        agent.handle_incoming_message(db, conversation, "biryani")

        # Turn 4: picks the restaurant -> get_menu, then items with prices.
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
        agent.handle_incoming_message(db, conversation, "1")

        # Turn 5: "2 chicken biryani" -> add_to_cart, then asks for delivery address.
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

        assert len(sent) == 5
        for reply in sent:
            assert not agent._leaks_tool_call(reply)

        quantities = [line["quantity"] for line in conversation.cart["items"]]
        assert quantities == [2], "the biryani was added exactly once"
        assert conversation.cart["items"][0]["menu_item_id"] == chicken_biryani.id
