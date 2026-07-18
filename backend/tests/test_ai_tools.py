"""The AI's tools.

These guard the things an LLM will otherwise get wrong with real money attached: it
invents prices, it invents item ids, and it re-runs the whole ordering flow when it
loses track of what it already did. Every test here corresponds to a bug that actually
happened during development.
"""

from decimal import Decimal

from sqlalchemy import select

from app.models import Conversation, MenuItem, Order, OrderStatus, PaymentStatus
from app.services import tools


class TestListRestaurants:
    def test_lists_open_restaurants(self, db, conversation):
        result = tools.list_restaurants(db, conversation)
        assert len(result["restaurants"]) >= 1

    def test_filters_by_cuisine(self, db, conversation):
        result = tools.list_restaurants(db, conversation, cuisine="pizza")
        assert [r["name"] for r in result["restaurants"]] == ["Pizza Junction"]

    def test_hides_restaurant_that_paused_orders(self, db, conversation, pizza):
        pizza.is_accepting_orders = False
        db.flush()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert "Pizza Junction" not in names


class TestSearchRestaurantsByItem:
    """Backs the new flow step where the customer names a dish before a restaurant —
    the AI must find who actually serves it via a real DB query, never a guess."""

    def test_finds_restaurant_serving_the_dish(self, db, conversation, biryani):
        result = tools.search_restaurants_by_item(db, conversation, query="biryani")
        names = [r["name"] for r in result["restaurants"]]
        assert names == ["Karachi Biryani House"]
        assert any("Biryani" in item for item in result["restaurants"][0]["matched_items"])

    def test_is_case_insensitive_substring(self, db, conversation, pizza):
        result = tools.search_restaurants_by_item(db, conversation, query="PIZZA")
        assert "Pizza Junction" in [r["name"] for r in result["restaurants"]]

    def test_no_match_returns_empty_with_a_note(self, db, conversation):
        result = tools.search_restaurants_by_item(db, conversation, query="sushi")
        assert result["restaurants"] == []
        assert "note" in result

    def test_empty_query_is_a_clean_error(self, db, conversation):
        assert "error" in tools.search_restaurants_by_item(db, conversation, query="")

    def test_paused_restaurant_is_excluded(self, db, conversation, biryani):
        biryani.is_accepting_orders = False
        db.flush()
        result = tools.search_restaurants_by_item(db, conversation, query="biryani")
        assert result["restaurants"] == []


class TestGetMenu:
    def test_by_id(self, db, conversation, pizza):
        result = tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert result["restaurant"]["name"] == "Pizza Junction"
        assert len(result["items"]) > 0

    def test_by_name(self, db, conversation):
        """The model persistently sends the NAME where the schema asked for an integer.
        Rejecting it made Groq 400 the whole call and the customer got a dead turn."""
        result = tools.get_menu(db, conversation, restaurant_id="Pizza Junction")
        assert result["restaurant"]["name"] == "Pizza Junction"

    def test_by_partial_lowercase_name(self, db, conversation):
        assert "items" in tools.get_menu(db, conversation, restaurant_name="pizza")

    def test_by_numeric_string_id(self, db, conversation, pizza):
        assert "items" in tools.get_menu(db, conversation, restaurant_id=str(pizza.id))

    def test_unknown_restaurant_is_a_clean_error(self, db, conversation):
        result = tools.get_menu(db, conversation, restaurant_name="Sushi Palace")
        assert result["error"] == "unknown_restaurant"

    def test_items_are_grouped_by_category(self, db, conversation, pizza):
        """sort_order restarts at 0 in each category, so sorting on it alone interleaved
        pizzas with milkshakes and the AI read out a jumbled menu."""
        items = tools.get_menu(db, conversation, restaurant_id=pizza.id)["items"]
        categories = [i["category"] for i in items]
        assert categories == sorted(categories, key=lambda c: categories.index(c)), (
            "items from one category must be contiguous"
        )

    def test_closed_restaurant_refuses(self, db, conversation, pizza):
        pizza.is_accepting_orders = False
        db.flush()
        assert tools.get_menu(db, conversation, restaurant_id=pizza.id)["error"] == "closed"


class TestAddToCart:
    def test_adds_item_at_the_database_price(self, db, conversation, pizza, menu_item):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        result = tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)

        line = result["cart"][0]
        assert Decimal(line["price"]) == menu_item.price
        assert Decimal(result["subtotal"]) == menu_item.price * 2

    def test_quantity_increment_survives_a_flush(self, db, conversation, pizza, menu_item):
        """Real customer impact (conv#643): the model told the customer '2 biryanis,
        Rs. 900', but the second add_to_cart's quantity increment never reached the
        DB. On the next turn, place_order loaded cart=1 (Rs. 450), tripped the
        below_minimum guard, and the order silently failed.

        Root cause: add_to_cart used to mutate the existing line dict IN PLACE
        (line['quantity'] += 1). SQLAlchemy's JSONB column has no MutableDict
        wrapper, so by the time the tool reassigned conversation.cart, the
        'new' value was structurally identical to the (already-mutated) current
        value — SQLAlchemy saw no change and skipped the UPDATE.

        This test flushes and expires between adds, forcing a fresh read from
        the DB. In-memory-only assertions never caught this."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)

        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        db.flush()
        db.expire(conversation)
        assert conversation.cart["items"][0]["quantity"] == 1, "first add did not persist"

        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        db.flush()
        db.expire(conversation)
        assert conversation.cart["items"][0]["quantity"] == 2, (
            "quantity increment was lost between turns — place_order will see the "
            "wrong subtotal"
        )

    def test_rejects_an_item_the_model_never_saw(self, db, conversation, pizza):
        """The model has been caught inventing plausible ids (1, 2, 12345) and ordering
        food from the wrong restaurant. An id is only valid if get_menu returned it."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)

        other = db.scalar(select(MenuItem).where(MenuItem.name == "Chicken Biryani"))
        result = tools.add_to_cart(db, conversation, menu_item_id=other.id)

        assert result["error"] == "unknown_item"
        assert conversation.cart["items"] == []

    def test_rejects_item_id_before_any_menu_was_shown(self, db, conversation, menu_item):
        assert tools.add_to_cart(db, conversation, menu_item_id=menu_item.id)["error"] == (
            "unknown_item"
        )

    def test_cart_is_locked_to_one_restaurant(self, db, conversation, pizza, biryani, menu_item):
        """Browsing a second restaurant mid-cart must not let its items in.

        This was a real bug: the guard compared against conversation.active_restaurant_id,
        which get_menu overwrites — so "add a pizza, then look at the biryani place, then
        add a biryani" sailed straight through.
        """
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id)

        tools.get_menu(db, conversation, restaurant_id=biryani.id)   # the customer browses
        biryani_item = db.scalar(
            select(MenuItem).where(MenuItem.restaurant_id == biryani.id)
        )
        result = tools.add_to_cart(db, conversation, menu_item_id=biryani_item.id)

        assert result["error"] == "cart_has_other_restaurant"
        assert len(conversation.cart["items"]) == 1

    def test_order_is_billed_to_the_carts_restaurant_not_the_browsed_one(
        self, db, conversation, pizza, biryani, menu_item
    ):
        """The nastier half of the same bug: place_order took the restaurant from
        active_restaurant_id, so a pizza order could be sent to a biryani house — with
        that restaurant's delivery fee and commission rate."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)

        tools.get_menu(db, conversation, restaurant_id=biryani.id)   # browses elsewhere
        result = tools.place_order(db, conversation, delivery_address="House 1, Lahore")
        db.flush()

        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
        assert order.restaurant_id == pizza.id
        assert order.commission_rate == pizza.commission_rate
        assert order.delivery_fee == pizza.delivery_fee

    def test_rejects_absurd_quantity(self, db, conversation, pizza, menu_item):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert "error" in tools.add_to_cart(
            db, conversation, menu_item_id=menu_item.id, quantity=999
        )

    def test_unavailable_item_cannot_be_ordered(self, db, conversation, pizza, menu_item):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        menu_item.is_available = False
        db.flush()
        assert "error" in tools.add_to_cart(db, conversation, menu_item_id=menu_item.id)


class TestPlaceOrder:
    def test_prices_come_from_the_database(self, db, cod_order, menu_item):
        """The model passes ids and quantities, never prices. Otherwise a customer can
        talk it into a Rs. 1 pizza."""
        line = cod_order.items[0]
        assert line.price_at_order == menu_item.price
        assert line.line_total == menu_item.price * line.quantity
        assert cod_order.subtotal == sum(i.line_total for i in cod_order.items)

    def test_totals_and_commission(self, db, cod_order, pizza):
        assert cod_order.total_amount == cod_order.subtotal + cod_order.delivery_fee
        assert cod_order.commission_rate == pizza.commission_rate
        assert cod_order.commission_amount == (
            cod_order.subtotal * pizza.commission_rate / Decimal("100")
        ).quantize(Decimal("0.01"))

    def test_empty_cart_is_refused(self, db, conversation):
        assert "error" in tools.place_order(db, conversation, delivery_address="X")

    def test_address_is_required(self, db, cart_with_pizza):
        result = tools.place_order(db, cart_with_pizza)
        assert result["error"] == "missing_address"

    def test_below_minimum_is_refused(self, db, conversation, pizza, menu_item):
        pizza.min_order_amount = Decimal("99999.00")
        db.flush()

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id)
        result = tools.place_order(db, conversation, delivery_address="House 1")

        assert result["error"] == "below_minimum"

    def test_cart_is_emptied_after_ordering(self, db, cod_order, conversation):
        assert conversation.cart["items"] == []

    def test_item_snapshot_survives_menu_changes(self, db, cod_order, menu_item):
        """Renaming or repricing the menu must not rewrite order history."""
        original_name = cod_order.items[0].item_name
        menu_item.name = "Renamed Pizza"
        menu_item.price = Decimal("1.00")
        db.flush()
        db.refresh(cod_order)

        assert cod_order.items[0].item_name == original_name
        assert cod_order.items[0].price_at_order != Decimal("1.00")

    def test_duplicate_order_is_prevented(self, db, conversation, pizza, menu_item):
        """'Where is my order?' once made the model re-run the whole flow and place a
        SECOND order. This is the last line of defence against a double charge."""
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)
        first = tools.place_order(db, conversation, delivery_address="House 1")
        db.flush()

        # The model rebuilds the identical cart and orders again moments later.
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)
        second = tools.place_order(db, conversation, delivery_address="House 1")

        assert second.get("duplicate_prevented") is True
        assert second["order_number"] == first["order_number"]

        orders = db.scalars(
            select(Order).where(Order.customer_id == conversation.customer_id)
        ).all()
        assert len(orders) == 1


class TestOrderStatusTool:
    def test_reports_the_latest_order(self, db, conversation, cod_order):
        result = tools.get_order_status(db, conversation)
        assert result["order_number"] == cod_order.order_number
        assert result["status"] == OrderStatus.PENDING.value

    def test_by_order_number(self, db, conversation, cod_order):
        result = tools.get_order_status(db, conversation, order_number=cod_order.order_number)
        assert result["total"] == f"{cod_order.total_amount:.2f}"

    def test_unknown_order(self, db, conversation):
        assert "error" in tools.get_order_status(db, conversation, order_number="AB-NOPE")

    def test_cannot_see_another_customers_order(self, db, cod_order):
        from app.services import conversations as convo

        other = convo.get_or_create_customer(db, "923009999999")
        other_conv = convo.get_or_create_conversation(db, other)
        db.flush()

        assert "error" in tools.get_order_status(
            db, other_conv, order_number=cod_order.order_number
        )
