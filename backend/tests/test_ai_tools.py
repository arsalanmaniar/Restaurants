"""The AI's tools.

These guard the things an LLM will otherwise get wrong with real money attached: it
invents prices, it invents item ids, and it re-runs the whole ordering flow when it
loses track of what it already did. Every test here corresponds to a bug that actually
happened during development.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.models import Conversation, MenuItem, Order, OrderRating, OrderStatus, PaymentStatus
from app.services import ranking, tools


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
        # Phase F — the total now includes tax. cod_order is COD, so 15% on the
        # food subtotal (no coupon here, so taxable food == subtotal).
        expected_tax = (cod_order.subtotal * Decimal("15") / Decimal("100")).quantize(
            Decimal("0.01")
        )
        assert cod_order.tax_rate == Decimal("15.00")
        assert cod_order.tax_amount == expected_tax
        assert cod_order.total_amount == (
            cod_order.subtotal + expected_tax + cod_order.delivery_fee
        )
        # Commission base is subtotal + tax (delivery excluded), then − discount.
        assert cod_order.commission_rate == pizza.commission_rate
        assert cod_order.commission_amount == (
            (cod_order.subtotal + expected_tax) * pizza.commission_rate / Decimal("100")
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


class TestFindPastOrder:
    """Turn "my last biryani" / "the office lunch" / "Eid order" into a specific
    past order. Item-name matches, notes matches, customer isolation, ambiguity
    handling — all pinned so a future refactor can't quietly loosen the privacy
    scope or the ranking."""

    def test_matches_by_item_name(self, db, conversation, cod_order):
        # cod_order fixture places a Chicken Tikka Pizza — search for "pizza"
        # must surface it.
        result = tools.find_past_order(db, conversation, "pizza")
        assert result["query"] == "pizza"
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["order_number"] == cod_order.order_number
        assert any(
            "pizza" in item["name"].lower()
            for item in result["candidates"][0]["items"]
        )

    def test_matches_by_order_notes(self, db, conversation, cod_order):
        # A restaurant might not have "eid" in an item name — the customer's
        # own note is where a phrase-based memory lives.
        cod_order.notes = "Eid dinner for family"
        db.flush()

        result = tools.find_past_order(db, conversation, "Eid")
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["order_number"] == cod_order.order_number
        assert result["candidates"][0]["notes"] == "Eid dinner for family"

    def test_case_insensitive(self, db, conversation, cod_order):
        for term in ("PIZZA", "Pizza", "pIzZa"):
            result = tools.find_past_order(db, conversation, term)
            assert len(result["candidates"]) == 1, f"failed on {term!r}"

    def test_no_match_returns_empty_with_note(self, db, conversation, cod_order):
        result = tools.find_past_order(db, conversation, "chowmein")
        assert result["candidates"] == []
        assert "chowmein" in result["note"]

    def test_empty_query_is_an_error(self, db, conversation):
        assert "error" in tools.find_past_order(db, conversation, "")
        assert "error" in tools.find_past_order(db, conversation, "   ")

    def test_only_this_customer_sees_their_orders(self, db, cod_order):
        """Privacy boundary — another customer's search must NEVER surface
        this order, even with a keyword that would obviously match its items."""
        from app.services import conversations as convo

        other = convo.get_or_create_customer(db, "923009999999")
        other_conv = convo.get_or_create_conversation(db, other)
        db.flush()

        result = tools.find_past_order(db, other_conv, "pizza")
        assert result["candidates"] == []

    def test_multiple_matches_return_newest_first(
        self, db, conversation, cart_with_pizza, pizza, menu_item
    ):
        """Two orders for this customer, both with pizza — the newest one must
        come first so the model can lead with 'was this last Tuesday's order?'."""
        first = tools.place_order(
            db, cart_with_pizza, delivery_address="House 1, Lahore"
        )
        db.flush()

        # Rebuild the cart and place a second pizza order.
        tools.get_menu(db, cart_with_pizza, restaurant_id=pizza.id)
        tools.add_to_cart(db, cart_with_pizza, menu_item_id=menu_item.id, quantity=1)
        second = tools.place_order(
            db, cart_with_pizza, delivery_address="House 2, Lahore"
        )
        db.flush()

        result = tools.find_past_order(db, cart_with_pizza, "pizza")
        assert len(result["candidates"]) == 2
        # Newest-first ordering — the second call placed the more recent order.
        assert result["candidates"][0]["order_number"] == second["order_number"]
        assert result["candidates"][1]["order_number"] == first["order_number"]

    def test_result_shape_is_stable(self, db, conversation, cod_order):
        """Contract the model relies on — if these field names change the
        prompt guidance needs to change with them."""
        result = tools.find_past_order(db, conversation, "pizza")
        candidate = result["candidates"][0]
        for field in (
            "order_number", "restaurant", "restaurant_id",
            "placed_at", "total", "status", "notes", "items",
        ):
            assert field in candidate, f"missing {field!r} in candidate shape"
        assert isinstance(candidate["items"], list)
        assert "name" in candidate["items"][0]
        assert "quantity" in candidate["items"][0]


KARACHI = ZoneInfo("Asia/Karachi")


def _rate(db, order, customer, restaurant_id, rating: int):
    """Attach a rating to an existing order — used to seed high/low rated
    restaurants so the ranking formula has real numbers to work with."""
    db.add(OrderRating(
        order_id=order.id,
        restaurant_id=restaurant_id,
        customer_id=customer.id,
        rating=rating,
        source="test",
    ))
    db.flush()


class TestRestaurantRanking:
    """Ranking = 3*relevance + 2*rating + rotation_seed. Pins each signal
    independently, plus the two invariants that matter most in a live system:
    (a) same day → same order (deterministic), (b) different days → different
    top slot (rotation actually rotates)."""

    def test_ranking_note_is_attached_to_each_result(self, db, conversation):
        result = tools.list_restaurants(db, conversation)
        assert result["restaurants"], "seed data has no open restaurants"
        for entry in result["restaurants"]:
            assert "ranking_note" in entry
            assert entry["ranking_note"], "note must not be empty"

    def test_search_by_item_attaches_ranking_note(self, db, conversation):
        result = tools.search_restaurants_by_item(db, conversation, query="pizza")
        for entry in result["restaurants"]:
            assert "ranking_note" in entry
            # Search matches carry the item-name in the reason string
            assert "serves" in entry["ranking_note"].lower()

    def test_higher_rated_restaurant_ranks_above_unrated(self, db, conversation, pizza, biryani, cod_order, customer):
        # cod_order is a Pizza Junction order — rate it 5 stars
        _rate(db, cod_order, customer, pizza.id, 5)

        result = tools.list_restaurants(db, conversation)
        names = [r["name"] for r in result["restaurants"]]

        # Pizza Junction (5 stars) must rank strictly above Karachi Biryani
        # House (no ratings, at the neutral prior 0.5).
        assert "Pizza Junction" in names
        assert "Karachi Biryani House" in names
        assert names.index("Pizza Junction") < names.index("Karachi Biryani House")

    def test_ranking_is_deterministic_within_a_day(self, db, conversation):
        a = tools.list_restaurants(db, conversation)
        b = tools.list_restaurants(db, conversation)
        assert [r["name"] for r in a["restaurants"]] == [r["name"] for r in b["restaurants"]]

    def test_rotation_actually_rotates_across_days(self, db, pizza, biryani):
        """The whole point of the daily rotation is that the top slot changes
        day to day, not that it's random per request. Sample many days and
        confirm at least two different names lead."""
        from app.models import Restaurant
        from sqlalchemy import select

        candidates = db.scalars(select(Restaurant)).all()
        assert len(candidates) >= 2, "need multiple restaurants for rotation to matter"

        base = datetime(2026, 1, 1, 12, 0, tzinfo=KARACHI)
        top_names_by_day = set()
        for day_offset in range(0, 30):
            at = base + timedelta(days=day_offset)
            ranked = ranking.rank_restaurants(db, candidates, at=at)
            top_names_by_day.add(ranked[0].restaurant.name)

        assert len(top_names_by_day) >= 2, (
            f"rotation is broken — same restaurant led for 30 days straight "
            f"({top_names_by_day})"
        )

    def test_relevance_beats_rating(self, db, conversation, pizza, biryani, cod_order, customer):
        """A matched search result must outrank a highly-rated non-match — a
        biryani query cannot return Pizza Junction just because it has 5 stars.
        Pins the '3*relevance vs 2*rating' weight choice."""
        _rate(db, cod_order, customer, pizza.id, 5)

        # search_restaurants_by_item only returns MATCHED restaurants, so this
        # test is really "make sure Pizza Junction doesn't sneak into a biryani
        # search". Belt-and-braces against a future 'return all if no match'
        # regression that would defeat relevance entirely.
        result = tools.search_restaurants_by_item(db, conversation, query="biryani")
        names = [r["name"] for r in result["restaurants"]]
        assert "Pizza Junction" not in names, (
            "biryani search must not return Pizza Junction, even with 5-star rating"
        )
        assert "Karachi Biryani House" in names

    def test_rotation_seed_is_bounded(self):
        """The rotation weight must never overwhelm rating (2.0). Sample every
        rotation seed for a big range of (restaurant_id, day) pairs and confirm
        it stays inside the documented [0, 0.1) window."""
        from app.services.ranking import ROTATION_MAX, _rotation_seed

        from datetime import date
        for rid in range(1, 100):
            for day_offset in range(0, 400):
                seed = _rotation_seed(rid, date.fromordinal(730000 + day_offset))
                assert 0.0 <= seed < ROTATION_MAX, f"seed {seed} out of range for {rid=} {day_offset=}"

    def test_no_customer_data_leaks_into_ranking(self, db, conversation, pizza, biryani, cod_order, customer):
        """Ranking must not depend on which customer is asking — otherwise a
        restaurant could be ranked higher for one customer than another based
        on their own past orders, which is not what we want here (that's the
        personalisation feature, a separate scoped change later)."""
        from app.services import conversations as convo

        _rate(db, cod_order, customer, pizza.id, 5)

        other = convo.get_or_create_customer(db, "923004440000")
        other_conv = convo.get_or_create_conversation(db, other)
        db.flush()

        a = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        b = [r["name"] for r in tools.list_restaurants(db, other_conv)["restaurants"]]
        assert a == b
