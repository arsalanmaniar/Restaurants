"""Reorder last order.

Must re-validate everything against the CURRENT menu — never resurrect a stale
price — and must go through the same grounding guard as a human-driven order
(add_to_cart refuses any item id that get_menu hasn't shown this conversation).
"""

from decimal import Decimal

from sqlalchemy import select

from app.models import MenuItem
from app.services import tools


class TestNoPreviousOrder:
    def test_clean_error_when_the_customer_never_ordered(self, db, conversation):
        result = tools.reorder_last(db, conversation)
        assert result["error"] == "no_previous_order"


class TestBasicReorder:
    def test_reorders_the_same_item_at_the_current_price(
        self, db, cod_order, conversation, menu_item
    ):
        result = tools.reorder_last(db, conversation)
        assert "error" not in result, result

        assert result["added"][0]["name"] == menu_item.name
        assert Decimal(result["added"][0]["price"]) == menu_item.price
        assert result["dropped"] == []

        line = conversation.cart["items"][0]
        assert line["menu_item_id"] == menu_item.id
        assert Decimal(line["price"]) == menu_item.price
        assert line["quantity"] == cod_order.items[0].quantity

    def test_grounding_context_is_populated_exactly_like_get_menu(
        self, db, cod_order, conversation, menu_item
    ):
        """reorder_last must not bypass the add_to_cart guard — it has to go through
        get_menu so shown_menu_ids is populated for real."""
        tools.reorder_last(db, conversation)
        assert menu_item.id in conversation.context["shown_menu_ids"]
        assert conversation.context["shown_menu_restaurant"] == menu_item.restaurant.name

    def test_reorder_replaces_whatever_was_already_in_the_cart(
        self, db, cod_order, conversation, pizza, menu_item
    ):
        # Put something unrelated in the cart first.
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        other_item = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id, MenuItem.name.ilike("%Pepperoni%")
            )
        )
        tools.add_to_cart(db, conversation, menu_item_id=other_item.id, quantity=3)

        tools.reorder_last(db, conversation)

        item_ids = {line["menu_item_id"] for line in conversation.cart["items"]}
        assert item_ids == {menu_item.id}


class TestRepricedItem:
    def test_uses_the_current_price_never_the_old_one(
        self, db, cod_order, conversation, menu_item
    ):
        original_price = cod_order.items[0].price_at_order
        menu_item.price = original_price + Decimal("500.00")
        db.flush()

        result = tools.reorder_last(db, conversation)

        assert Decimal(result["added"][0]["price"]) == menu_item.price
        assert Decimal(result["added"][0]["price"]) != original_price
        assert Decimal(conversation.cart["items"][0]["price"]) == menu_item.price


class TestDroppedItems:
    def test_deleted_item_leaves_nothing_to_reorder(
        self, db, cod_order, conversation, menu_item
    ):
        """cod_order has exactly one line, and it's the item being deleted."""
        item_name = menu_item.name
        db.delete(menu_item)
        db.flush()

        result = tools.reorder_last(db, conversation)
        assert result["error"] == "nothing_to_reorder"
        assert any(d["name"] == item_name for d in result["dropped"])

    def test_one_deleted_item_among_several_is_dropped_but_the_rest_reorder(
        self, db, conversation, pizza
    ):
        kept = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id, MenuItem.name.ilike("%Chicken Tikka Pizza%")
            )
        )
        removed = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id, MenuItem.name.ilike("%Pepperoni%")
            )
        )

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=kept.id, quantity=1)
        tools.add_to_cart(db, conversation, menu_item_id=removed.id, quantity=1)
        tools.place_order(db, conversation, delivery_address="House 1, DHA, Lahore")
        db.flush()

        removed_name = removed.name
        db.delete(removed)
        db.flush()

        result = tools.reorder_last(db, conversation)
        assert "error" not in result, result

        added_names = {a["name"] for a in result["added"]}
        assert kept.name in added_names
        assert removed_name not in added_names
        assert any(d["name"] == removed_name for d in result["dropped"])

    def test_unavailable_item_is_dropped_and_reported(self, db, cod_order, conversation, menu_item):
        item_name = menu_item.name
        menu_item.is_available = False
        db.flush()

        result = tools.reorder_last(db, conversation)
        assert result["error"] == "nothing_to_reorder"
        assert any(d["name"] == item_name for d in result["dropped"])


class TestClosedRestaurant:
    def test_refuses_entirely_when_the_restaurant_is_now_closed(
        self, db, cod_order, conversation, pizza
    ):
        pizza.is_accepting_orders = False
        db.flush()

        result = tools.reorder_last(db, conversation)
        assert result["error"] == "restaurant_closed"
        # Nothing was rebuilt — the cart stays exactly as it was (empty, post-order).
        assert conversation.cart["items"] == []

    def test_refuses_when_the_restaurant_was_suspended(self, db, cod_order, conversation, pizza):
        from app.models import RestaurantStatus

        pizza.status = RestaurantStatus.SUSPENDED
        db.flush()

        result = tools.reorder_last(db, conversation)
        assert result["error"] == "restaurant_closed"
