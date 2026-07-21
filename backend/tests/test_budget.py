"""Budget-aware recommendations — `estimate_meal_cost` + the extended
`find_restaurants(budget, party_size)` signature.

Cases pinned here mirror the four verification points from the Phase 6 plan:

  1. Budget-in-Rupees query returns options with an `estimated_total`
     (representative meal cost + delivery), not just raw item prices.
  2. When NOTHING fits the budget, the response is graceful — every entry
     has `fits_budget: false`, a top-level `note` names the cheapest
     option, and the model is instructed to offer to broaden the budget.
  3. `party_size` scales the estimate linearly (2 people → 2× food cost,
     same delivery fee).
  4. The tool remains read-only and backward-compatible — no budget /
     party_size → identical response to Phase 5.
"""

from decimal import Decimal

from app.services import discovery as discovery_service
from app.services import tools


# --------------------------------------------------------------------------- #
# Pure service — estimate_meal_cost
# --------------------------------------------------------------------------- #


class TestEstimateMealCost:
    def test_empty_matched_returns_none(self):
        assert discovery_service.estimate_meal_cost(
            matched_menu_items=[],
            delivery_fee=Decimal("100"),
            min_order_amount=Decimal("500"),
        ) is None

    def test_cheapest_item_wins(self, db, biryani):
        """Given two matched items, the estimate is based on the CHEAPER
        one — customer's budget question ("kya fit hoga?") wants a lower
        bound, not an average."""
        from sqlalchemy import select
        from app.models import MenuItem
        chicken = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Chicken Biryani",  # Rs. 450
        ))
        beef = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Beef Biryani",  # Rs. 550
        ))
        estimate = discovery_service.estimate_meal_cost(
            matched_menu_items=[beef, chicken],
            delivery_fee=Decimal("80"),
            min_order_amount=Decimal("500"),
        )
        assert estimate["primary_item"]["name"] == "Chicken Biryani"
        assert estimate["primary_item"]["price"] == "450.00"

    def test_party_size_scales_food_linearly(self, db, biryani):
        """4 × Chicken Biryani (Rs. 450 each) = Rs. 1800 food + Rs. 80
        delivery = Rs. 1880 total. Delivery is per-order, not per-person."""
        from sqlalchemy import select
        from app.models import MenuItem
        chicken = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Chicken Biryani",
        ))
        estimate = discovery_service.estimate_meal_cost(
            matched_menu_items=[chicken],
            delivery_fee=Decimal("80"),
            min_order_amount=Decimal("500"),
            party_size=4,
        )
        assert estimate["party_size"] == 4
        assert estimate["food_estimate"] == "1800.00"
        assert estimate["delivery_fee"] == "80.00"
        assert estimate["estimated_total"] == "1880.00"

    def test_below_minimum_is_clamped_to_min_order(self, db, biryani):
        """1 × Raita (Rs. 80) is well under Karachi Biryani House's Rs. 500
        min order, so the estimate must round UP to the minimum so the model
        doesn't tell the customer "fits in Rs. 100" and then place_order
        errors with below_minimum. Realistic order semantics."""
        from sqlalchemy import select
        from app.models import MenuItem
        raita = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Raita",  # Rs. 80
        ))
        estimate = discovery_service.estimate_meal_cost(
            matched_menu_items=[raita],
            delivery_fee=Decimal("80"),
            min_order_amount=Decimal("500"),
        )
        # Food clamped up from Rs. 80 to Rs. 500 (the minimum)
        assert estimate["food_estimate"] == "500.00"
        # Total = 500 + 80 = 580
        assert estimate["estimated_total"] == "580.00"

    def test_party_size_less_than_one_treated_as_one(self, db, biryani):
        """Defensive: a bogus party_size (0, -3) is coerced to 1 rather
        than producing a zero-cost estimate the model might quote."""
        from sqlalchemy import select
        from app.models import MenuItem
        chicken = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Chicken Biryani",
        ))
        estimate = discovery_service.estimate_meal_cost(
            matched_menu_items=[chicken],
            delivery_fee=Decimal("80"),
            min_order_amount=Decimal("500"),
            party_size=0,
        )
        assert estimate["party_size"] == 1
        assert estimate["food_estimate"] == "500.00"  # 450 clamped to min


# --------------------------------------------------------------------------- #
# Agent tool — find_restaurants with budget / party_size
# --------------------------------------------------------------------------- #


class TestFindRestaurantsWithBudget:
    def test_case1_budget_query_returns_estimate_per_restaurant(
        self, db, conversation, biryani,
    ):
        """CASE 1 — customer says a budget → tool returns estimate + fits_budget."""
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=1500,
        )
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert "estimate" in biryani_row
        assert biryani_row["estimate"]["primary_item"]["name"] == "Chicken Biryani"
        # 1 × Chicken Biryani (Rs. 450) + Rs. 80 delivery = Rs. 530.
        # But that's under the Rs. 500 min order — food clamps to 500,
        # total = 500 + 80 = 580.
        assert biryani_row["estimate"]["estimated_total"] == "580.00"
        assert biryani_row["fits_budget"] is True
        assert result["budget"] == "1500.00"
        assert result["party_size"] == 1

    def test_case2_nothing_fits_returns_graceful_note(
        self, db, conversation,
    ):
        """CASE 2 — budget too low → every entry has fits_budget=false AND
        a top-level note quotes the cheapest option so the model can offer
        to broaden the budget."""
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=100,
        )
        assert len(result["restaurants"]) >= 1
        assert all(r.get("fits_budget") is False for r in result["restaurants"])
        assert "note" in result
        # Note names a cheapest option so the model can quote it
        assert "cheapest" in result["note"].lower()
        assert "Rs." in result["note"]

    def test_case3_party_size_scales_the_estimate(
        self, db, conversation, biryani,
    ):
        """CASE 3 — party_size=6 → 6 × cheapest matched item + delivery,
        clamped to minimum. 6 × Rs. 450 = Rs. 2700 (well above min) +
        Rs. 80 delivery = Rs. 2780."""
        result = tools.find_restaurants(
            db, conversation,
            query="biryani", budget=3000, party_size=6,
        )
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert biryani_row["estimate"]["party_size"] == 6
        assert biryani_row["estimate"]["food_estimate"] == "2700.00"
        assert biryani_row["estimate"]["estimated_total"] == "2780.00"
        assert biryani_row["fits_budget"] is True  # 2780 <= 3000

    def test_case4_backward_compat_no_budget_no_estimate_no_regression(
        self, db, conversation, biryani,
    ):
        """CASE 4 — omitting budget/party_size gives back the Phase 5
        response shape unchanged. No `estimate`, no `fits_budget`, no
        `budget`, no `party_size`, no `note` in the success case. All
        existing callers keep working."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        # Structural: keys that budget mode adds must be absent
        assert "budget" not in result
        assert "party_size" not in result
        assert "note" not in result  # only added on the nothing-fits path
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        # Per-restaurant budget keys also absent
        assert "estimate" not in biryani_row
        assert "fits_budget" not in biryani_row


class TestFindRestaurantsBudgetEdgeCases:
    def test_budget_zero_rejected(self, db, conversation):
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=0,
        )
        assert result.get("error") == "invalid_budget"

    def test_budget_negative_rejected(self, db, conversation):
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=-100,
        )
        assert result.get("error") == "invalid_budget"

    def test_budget_as_numeric_string_accepted(self, db, conversation, biryani):
        """The model has been observed sending numbers as strings — accept
        both shapes rather than fail the whole turn."""
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget="1500",
        )
        assert "error" not in result
        assert result["budget"] == "1500.00"

    def test_budget_garbage_string_rejected(self, db, conversation):
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget="one thousand",
        )
        assert result.get("error") == "invalid_budget"

    def test_bare_budget_no_query_lists_every_open_restaurant(
        self, db, conversation,
    ):
        """CASE: '1000 mein kya milega' — no cuisine hint but a budget.
        Should return every open restaurant with its own estimate against
        that budget."""
        result = tools.find_restaurants(db, conversation, budget=1000)
        names = {r["name"] for r in result["restaurants"]}
        # All three seed restaurants surface (the 3 open, active ones)
        assert names >= {
            "Karachi Biryani House", "Pizza Junction", "Wok & Roll",
        }
        # Every entry has an estimate + fits_budget flag
        for r in result["restaurants"]:
            assert "estimate" in r
            assert "fits_budget" in r

    def test_empty_query_and_no_budget_returns_error(self, db, conversation):
        """The one no-op path — nothing to search by."""
        result = tools.find_restaurants(db, conversation)
        assert result.get("error") == "empty_query"

    def test_cuisine_only_match_gets_estimate_from_cheapest_item(
        self, db, conversation,
    ):
        """'chinese' matches Wok & Roll purely via cuisine — no MenuItem
        row is named 'Chinese'. The estimate should fall back to the
        cheapest available item at Wok & Roll (Hot & Sour Soup, Rs. 320).
        Wok & Roll min_order=600, so food clamps to 600, total = 600 + 120
        = 720."""
        result = tools.find_restaurants(
            db, conversation, query="chinese", budget=1000,
        )
        wok = next(r for r in result["restaurants"] if r["name"] == "Wok & Roll")
        assert "estimate" in wok
        # Cheapest is Hot & Sour Soup Rs. 320, clamped to Rs. 600 min
        assert wok["estimate"]["food_estimate"] == "600.00"
        assert wok["estimate"]["estimated_total"] == "720.00"
        assert wok["fits_budget"] is True

    def test_estimate_reflects_min_order_clamp_for_borderline_budget(
        self, db, conversation, biryani,
    ):
        """A Rs. 500 budget doesn't fit biryani (estimated_total = Rs. 580
        after clamping). Verifies fits_budget takes the CLAMPED total,
        not raw item price, into account."""
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=500,
        )
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert biryani_row["fits_budget"] is False
        assert biryani_row["estimate"]["estimated_total"] == "580.00"

    def test_response_types_are_decimal_strings_not_floats(
        self, db, conversation, biryani,
    ):
        """Money values are Decimal-formatted strings ('580.00') so
        json.dumps doesn't introduce float noise ('580.0000000001')."""
        result = tools.find_restaurants(
            db, conversation, query="biryani", budget=1500,
        )
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        est = biryani_row["estimate"]
        for key in ("primary_item", "food_estimate", "delivery_fee", "estimated_total"):
            assert key in est
        for key in ("food_estimate", "delivery_fee", "estimated_total"):
            assert isinstance(est[key], str)
            assert "." in est[key]  # e.g. "580.00", never "580"

    def test_order_flow_still_works_with_budget_discovery(
        self, db, biryani,
    ):
        """CASE 4 — full end-to-end: budget query → menu → cart → place_order.
        Proves the read-only budget path doesn't disturb any state get_menu
        / add_to_cart / place_order rely on."""
        from app.models import CustomerAddress, MessageDirection
        from app.services import conversations as convo_svc

        customer = convo_svc.get_or_create_customer(db, "923055443322")
        db.add(CustomerAddress(
            customer_id=customer.id,
            address_text="House 12, Block B, Karachi",
            is_default=True,
        ))
        conv = convo_svc.get_or_create_conversation(db, customer)
        convo_svc.log_message(
            db, conv, MessageDirection.OUTBOUND,
            "Payment kis se karna hai — cod, jazzcash, ya easypaisa?",
        )
        db.flush()

        # Discovery via budget-aware find
        disc = tools.find_restaurants(db, conv, query="biryani", budget=1500)
        picked = next(r for r in disc["restaurants"] if r["id"] == biryani.id)
        assert picked["fits_budget"] is True

        # Continue normal flow
        menu = tools.get_menu(db, conv, restaurant_id=picked["id"])
        item = next(i for i in menu["items"] if i["name"] == "Chicken Biryani")
        add = tools.add_to_cart(db, conv, menu_item_id=item["id"], quantity=2)
        assert "error" not in add
        placed = tools.place_order(db, conv, payment_method="cod")
        assert "error" not in placed
        assert placed["order_number"].startswith("AB-")
