"""Intent-based restaurant discovery — services/discovery.py + the
`find_restaurants` agent tool.

Cases pinned here mirror the four verification points from the Phase 5 plan:

  1. Dish/cuisine keyword (e.g. "biryani", "pizza chahiye") lands on the
     right restaurant, no exact-name lookup required.
  2. A vaguer intent ("spicy", "chinese") also lands — the tool searches
     five columns including MenuItem.description and Restaurant.cuisine_type.
  3. The older `search_restaurants_by_item` and `list_restaurants` tools
     still work — backward-compat, deprecated in the schema description
     but not removed.
  4. The full menu → cart → address → payment → confirm flow still works
     unchanged (this tool only READS, never mutates).

Plus the correctness guarantees that make the discovery tool safe to trust:
open-only filtering, ranking wired in from Phase 2, and no leaked internal
tags in matched_items.
"""

import re
from decimal import Decimal

from sqlalchemy import delete, select, update

from app.models import MenuItem, RestaurantWorkingHours
from app.services import discovery as discovery_service
from app.services import tools


# --------------------------------------------------------------------------- #
# Pure service — find_matching_restaurants
# --------------------------------------------------------------------------- #


class TestFindMatchingRestaurants:
    """Directly against the ILIKE-across-five-columns search. Kept separate
    from the tool wrapper so search-shape bugs show up as focused failures."""

    def test_dish_name_match(self, db, biryani):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "biryani",
        )
        assert biryani.id in restaurants
        # Real menu item names (Chicken Biryani, Beef Biryani, Special Family
        # Biryani) — proves matched_items are truthful, not synthetic tags.
        assert any(
            "Biryani" in item for item in matched[biryani.id]
        )

    def test_cuisine_only_match_uses_cuisine_as_matched_signal(
        self, db, biryani,
    ):
        """A "desi" query matches biryani's cuisine_type='Desi' but no menu
        item literally contains 'desi'. matched_items falls back to the
        cuisine text so ranking still gives it the relevance boost."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "desi",
        )
        assert biryani.id in restaurants
        assert matched[biryani.id] == ["Desi"]  # truthful, quotable

    def test_description_only_match_via_menu_item_description(
        self, db, biryani,
    ):
        """'spicy' shows up in Beef Biryani's description ("Slow-cooked beef,
        spicy Sindhi masala") but NOT in any name. Menu-description search
        finds it and matched_items carries the real item name."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "spicy",
        )
        assert biryani.id in restaurants
        assert "Beef Biryani" in matched[biryani.id]

    def test_restaurant_description_match(self, db, pizza):
        """Pizza Junction's description says "Hand-tossed pizzas, loaded
        fries, and shakes." The word "hand-tossed" only lives in the
        restaurant description, no menu item mentions it — restaurant-level
        ILIKE catches it and cuisine text ("Pizza") becomes the matched
        signal."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "hand-tossed",
        )
        assert pizza.id in restaurants
        assert matched[pizza.id] == ["Pizza"]

    def test_query_matches_across_multiple_restaurants(self, db, biryani):
        """A single query can hit several restaurants — 'chicken' matches
        Chicken Biryani (biryani), Chicken Tikka Pizza (pizza), Chicken
        Chowmein (wok & roll). All three must appear."""
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "chicken",
        )
        # At least the biryani place, plus at least one other
        assert biryani.id in restaurants
        assert len(restaurants) >= 2

    def test_empty_query_returns_nothing(self, db):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(db, "")
        assert restaurants == {}
        assert matched == {}

    def test_no_match_returns_empty(self, db):
        restaurants, matched, _s, _b = discovery_service.find_matching_restaurants(
            db, "sushi-omakase-nowhere-xyz",
        )
        assert restaurants == {}
        assert matched == {}

    def test_unavailable_menu_items_are_ignored(self, db, pizza):
        """Menu items marked unavailable must not appear as matched_items —
        the customer would order something the kitchen can't make right now.
        The restaurant itself may still appear via its cuisine/description
        match (Pizza Junction's cuisine_type IS 'Pizza')."""
        # Kill every Pizza-named menu item; leave sides/drinks alone
        db.execute(
            MenuItem.__table__.update()
            .where(MenuItem.restaurant_id == pizza.id)
            .where(MenuItem.name.ilike("%Pizza%"))
            .values(is_available=False)
        )
        db.flush()

        _, matched, _s, _b = discovery_service.find_matching_restaurants(db, "pizza")
        # None of the disabled pizza items should appear in matched_items
        disabled = {
            "Chicken Tikka Pizza (Medium)",
            "Pepperoni Pizza (Medium)",
            "Fajita Pizza (Large)",
        }
        for items in matched.values():
            assert not (disabled & set(items))

    def test_closed_restaurant_is_filtered_out(self, db, biryani):
        """Restaurant matches the query but is closed — must not be
        offered. Achieved by wiping working hours (the always_open fixture
        installed them) and re-installing a schedule that excludes today."""
        # Wipe working hours for biryani entirely — with no hours, is_open()
        # returns False by design (see the always_open fixture docstring).
        db.execute(
            delete(RestaurantWorkingHours).where(
                RestaurantWorkingHours.restaurant_id == biryani.id
            )
        )
        db.flush()

        restaurants, _matched, _s, _b = discovery_service.find_matching_restaurants(db, "biryani")
        assert biryani.id not in restaurants


# --------------------------------------------------------------------------- #
# Agent tool — find_restaurants
# --------------------------------------------------------------------------- #


class TestFindRestaurantsTool:
    """The JSON shape the model sees. Covers the four Phase 5 verification
    cases end-to-end."""

    def test_case1_dish_query_lands_on_right_restaurant(
        self, db, conversation, biryani,
    ):
        """CASE 1 — customer says a dish word, tool matches without any
        exact restaurant-name lookup."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        assert result["query"] == "biryani"
        names = [r["name"] for r in result["restaurants"]]
        assert biryani.name in names
        # matched_items surface real menu item names
        biryani_row = next(r for r in result["restaurants"] if r["id"] == biryani.id)
        assert any("Biryani" in item for item in biryani_row["matched_items"])
        # ranking_note is present so the model can quote it verbatim
        assert biryani_row["ranking_note"]

    def test_case1_roman_urdu_dish_query(
        self, db, conversation, pizza,
    ):
        """'pizza' word inside 'pizza chahiye' still matches. The tool
        doesn't parse language — it treats the message as a keyword the
        model already extracted. Model would pass "pizza" here."""
        result = tools.find_restaurants(db, conversation, query="pizza")
        names = [r["name"] for r in result["restaurants"]]
        assert pizza.name in names

    def test_case2_vague_intent_spicy(self, db, conversation, biryani):
        """CASE 2 — 'spicy' matches menu-item descriptions ("spicy Sindhi
        masala") that aren't in any item NAME. Old search_restaurants_by_item
        would miss this; find_restaurants catches it."""
        result = tools.find_restaurants(db, conversation, query="spicy")
        assert result["query"] == "spicy"
        assert len(result["restaurants"]) >= 1
        # Biryani place must be one of them
        assert any(r["id"] == biryani.id for r in result["restaurants"])

    def test_case2_cuisine_query_matches_via_cuisine_field(
        self, db, conversation,
    ):
        """A cuisine word — "chinese" — matches Wok & Roll purely via
        Restaurant.cuisine_type='Chinese' (and its description "Fast
        Chinese"). No menu item literally contains "chinese"."""
        result = tools.find_restaurants(db, conversation, query="chinese")
        names = [r["name"] for r in result["restaurants"]]
        assert "Wok & Roll" in names
        wok_row = next(r for r in result["restaurants"] if r["name"] == "Wok & Roll")
        # matched_items fell back to cuisine text — customer-friendly, truthful
        assert wok_row["matched_items"] == ["Chinese"]

    def test_case3_search_restaurants_by_item_still_works(
        self, db, conversation, biryani,
    ):
        """CASE 3 — backward-compat: the legacy tool still returns a valid
        result shape. Deprecated in the schema, but the impl is untouched."""
        result = tools.search_restaurants_by_item(db, conversation, query="biryani")
        assert "restaurants" in result
        assert any(r["id"] == biryani.id for r in result["restaurants"])

    def test_case3_list_restaurants_still_works(self, db, conversation):
        """CASE 3 — the greeting-turn tool is unchanged."""
        result = tools.list_restaurants(db, conversation)
        # All three seed restaurants surfaced, in ranked order
        names = {r["name"] for r in result["restaurants"]}
        assert {"Karachi Biryani House", "Pizza Junction", "Wok & Roll"} <= names

    def test_case4_tool_does_not_touch_order_state(
        self, db, conversation,
    ):
        """CASE 4 — a plain discovery query must not touch anything the ORDER
        flow depends on: no cart change, no `shown_menu_ids` grounding, no
        active restaurant. Those may only be set by get_menu / add_to_cart,
        which is what stops a hallucinated item id becoming an order line.

        It DOES now record `shown_restaurants` (the candidate list it just
        presented) — that key is read only by the discovery path and by the
        agent's system message, never by the add_to_cart grounding guard."""
        before_cart = dict(conversation.cart or {})
        before_active = conversation.active_restaurant_id

        tools.find_restaurants(db, conversation, query="biryani")

        context = conversation.context or {}
        assert (conversation.cart or {}) == before_cart
        assert conversation.active_restaurant_id == before_active
        assert "shown_menu_ids" not in context
        assert "shown_menu" not in context

    def test_empty_query_returns_error(self, db, conversation):
        result = tools.find_restaurants(db, conversation, query="")
        assert result.get("error") == "empty_query"

    def test_unknown_query_returns_empty_with_definitive_note(
        self, db, conversation,
    ):
        """Was: the note pointed the model at a list_restaurants fallback.

        That instruction is gone. The search already covered every open
        restaurant, so the answer is definitive — and obeying the old note is
        what produced "no burger restaurants" immediately followed by a
        numbered list of restaurants. See test_match_quality.py.
        """
        result = tools.find_restaurants(
            db, conversation, query="sushi-omakase-nowhere-xyz",
        )
        assert result["restaurants"] == []
        assert result["found_anywhere"] is False
        assert "list_restaurants" not in result["note"]
        assert result["available_cuisines"]

    def test_result_uses_ranking_from_phase_2(
        self, db, conversation, biryani, pizza,
    ):
        """A matched query gets full relevance credit — restaurant with
        matched_items appears with a ranking_note that mentions the match.
        Proves the Phase 2 ranking is actually wired in, not bypassed."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        biryani_row = next(
            r for r in result["restaurants"] if r["id"] == biryani.id
        )
        # ranking_note reads like "serves Chicken Biryani, Beef Biryani (+1 more)"
        # — comes from ranking._build_reason, so this is the Phase 2 path.
        assert "serves" in biryani_row["ranking_note"].lower() or \
               "biryani" in biryani_row["ranking_note"].lower()

    def test_no_internal_tag_leak_in_matched_items(
        self, db, conversation,
    ):
        """matched_items must contain ONLY customer-facing text — real menu
        names or the cuisine text. Never an internal tag like 'cuisine
        match' or 'description match'."""
        for query in ("biryani", "chinese", "spicy", "hand-tossed"):
            result = tools.find_restaurants(db, conversation, query=query)
            for r in result.get("restaurants", []):
                for item in r["matched_items"]:
                    # No brackets, no colons, no square-bracket tags
                    assert "[" not in item
                    assert "match" not in item.lower()

    def test_matched_items_capped_per_restaurant(
        self, db, conversation, biryani,
    ):
        """Even if a query hits many menu items, matched_items is capped so
        the ranking reason doesn't become an essay ("(+42 more)")."""
        result = tools.find_restaurants(db, conversation, query="biryani")
        biryani_row = next(
            r for r in result["restaurants"] if r["id"] == biryani.id
        )
        assert len(biryani_row["matched_items"]) <= (
            discovery_service.MAX_MATCHED_ITEMS_PER_RESTAURANT
        )


# --------------------------------------------------------------------------- #
# A zero-match is global, in every branch of the note
# --------------------------------------------------------------------------- #


# Every branch must FORBID the redundant search in so many words.
_FORBIDS_SEARCHING_ELSEWHERE = re.compile(
    r"do not offer to (?:go )?(?:search|look) elsewhere", re.IGNORECASE
)

# ...and no sentence may ASK the customer about going elsewhere. This is what
# the old active-restaurant note did — "and ask whether they want you to look
# elsewhere" — so the check is scoped to one sentence (no "." in between) to
# keep it off the prohibition above, which never uses "ask".
_ASKS_ABOUT_ELSEWHERE = re.compile(r"ask\b[^.]{0,60}elsewhere", re.IGNORECASE)


class TestZeroMatchNoteIsGlobalInEveryContext:
    """The search behind `find_restaurants` is GLOBAL — it covers every open
    restaurant's name, cuisine, description and full menu, and is never scoped
    to the active restaurant or to the shortlist already shown. So a zero result
    means "nowhere", and every branch of `_empty_result_note` has to say so.

    Two branches used to narrow it. Production, with Karachi Biryani House
    active and "sandwich" available at zero restaurants:

        "Sandwich Karachi Biryani House mein nahi hai ... main aapke liye
         doosre restaurant se search karun?"

    Both halves came from the note: it framed the miss as local to one
    restaurant, then told the model to offer a search that had already run and
    returned nothing. The customer had to push back twice before hearing "kisi
    bhi restaurant mein available nahi hai".

    The query below matches nothing on purpose — the point of each test is the
    NOTE's framing, not whether the search works (covered above).
    """

    NOWHERE = "sushi-omakase-nowhere-xyz"

    def _note(self, db, conversation):
        result = tools.find_restaurants(db, conversation, query=self.NOWHERE)
        assert result["found_anywhere"] is False, "fixture query must match nothing"
        return result["note"]

    def _assert_global_and_final(self, note):
        """The two properties every branch owes the model: the answer covers ANY
        restaurant, and searching elsewhere is never on offer.

        The second is a POLARITY check, not a keyword check. Every branch now
        mentions "elsewhere" — that is how it FORBIDS the redundant search — so a
        bare `"elsewhere" not in note` would fail on the fix and pass on the bug.
        So: require the prohibition, and reject any sentence that ASKS about
        going elsewhere ("and ask whether they want you to look elsewhere").
        """
        assert "any of our restaurants" in note.lower()
        assert _FORBIDS_SEARCHING_ELSEWHERE.search(note), (
            f"note does not forbid the already-run search: {note!r}"
        )
        asks = _ASKS_ABOUT_ELSEWHERE.search(note)
        assert asks is None, f"note offers a search that already ran: {asks!r}"

    def test_first_contact_branch(self, db, conversation):
        """No active restaurant, nothing shown yet — the branch that was already
        correct. Pinned so the other two can be compared against it."""
        self._assert_global_and_final(self._note(db, conversation))

    def test_active_restaurant_branch_does_not_narrow_the_no(
        self, db, conversation, biryani,
    ):
        """THE PRODUCTION BUG. With a restaurant active the note used to say
        "say plainly if the item isn't on it, and ask whether they want you to
        look elsewhere" — scoping a global miss to one menu and inviting a
        redundant search. Both are gone; the continuity protection is not."""
        conversation.active_restaurant_id = biryani.id
        db.flush()

        note = self._note(db, conversation)

        self._assert_global_and_final(note)
        # Names the active restaurant only to warn AGAINST answering at that
        # scope — never as the scope of the "no" itself.
        assert "wrong here" in note.lower()
        assert biryani.name in note
        # Continuity protection from the original note must survive.
        assert "list_restaurants" in note

    def test_shortlist_branch_does_not_narrow_the_no(self, db, conversation):
        """Same narrowing, one step earlier: "say plainly that this wasn't found
        among them" understates a global zero-match to the shown shortlist."""
        conversation.context = {
            tools.SHOWN_RESTAURANTS_KEY: [{"id": 1, "name": "Karachi Biryani House"}]
        }
        db.flush()

        note = self._note(db, conversation)

        self._assert_global_and_final(note)
        assert "Karachi Biryani House" in note  # shortlist still preserved
        assert "list_restaurants" in note

    def test_active_restaurant_branch_still_beats_the_shortlist_branch(
        self, db, conversation, biryani,
    ):
        """Branch precedence is unchanged by this fix: an active restaurant wins
        over a shortlist, because the customer has already moved past it."""
        conversation.active_restaurant_id = biryani.id
        conversation.context = {
            tools.SHOWN_RESTAURANTS_KEY: [{"id": 999, "name": "Pizza Junction"}]
        }
        db.flush()

        note = self._note(db, conversation)

        assert biryani.name in note
        assert "Pizza Junction" not in note


# --------------------------------------------------------------------------- #
# Menu-less restaurants are never offered
# --------------------------------------------------------------------------- #


class TestMenulessRestaurantsAreNeverOffered:
    """A restaurant you cannot order from must never be put in front of a
    customer. Production hit this with Mandi House — a stub carrying 0 menu rows
    that was ACTIVE, accepting orders and open, so it appeared in the greeting.
    A customer picked it and got "Mandi House mein items available nahi hain",
    the bot contradicting an offer it had made one turn earlier.

    Approval and opening hours are necessary but NOT sufficient to be orderable;
    having something to sell is the other half.
    """

    def _stub(self, db, name="Empty Kitchen", cuisine="Mandi"):
        """An ACTIVE, accepting, always-open restaurant with NO menu at all."""
        from datetime import time

        from app.models import Restaurant, RestaurantStatus, RestaurantWorkingHours

        restaurant = Restaurant(
            name=name,
            phone="923004440077",
            cuisine_type=cuisine,
            description=f"Best {cuisine} in town.",
            status=RestaurantStatus.ACTIVE,
            commission_rate=Decimal("15.00"),
            is_accepting_orders=True,
        )
        db.add(restaurant)
        db.flush()
        # The autouse always_open fixture ran before this row existed, so give it
        # hours explicitly — otherwise it would be filtered as CLOSED and the test
        # would pass for the wrong reason.
        for day_of_week in range(7):
            db.add(RestaurantWorkingHours(
                restaurant_id=restaurant.id, day_of_week=day_of_week,
                opens_at=time(0, 0), closes_at=time(23, 59, 59), crosses_midnight=False,
            ))
        db.flush()
        return restaurant

    def test_it_really_is_open_and_active(self, db):
        """Guard the guard: prove the stub is excluded for having no MENU, not
        because it is closed or inactive."""
        from app.services.opening_hours import is_open

        stub = self._stub(db)
        assert is_open(stub) is True
        assert stub.is_accepting_orders is True

    def test_excluded_from_list_restaurants(self, db, conversation):
        stub = self._stub(db)
        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert stub.name not in names
        assert "Pizza Junction" in names, "restaurants WITH a menu must be unaffected"

    def test_excluded_from_discovery_matched_by_name(self, db, conversation):
        """The restaurant-level pass matches name/cuisine/description and ignores
        the menu — this is the path that would surface an empty restaurant."""
        stub = self._stub(db, name="Mandi Palace", cuisine="Mandi")
        result = tools.find_restaurants(db, conversation, query="Mandi Palace")
        assert stub.name not in [r["name"] for r in result.get("restaurants", [])]

    def test_excluded_from_discovery_matched_by_cuisine_or_description(
        self, db, conversation,
    ):
        stub = self._stub(db, name="Empty Grill", cuisine="Peri Peri")
        result = tools.find_restaurants(db, conversation, query="peri peri")
        assert stub.name not in [r["name"] for r in result.get("restaurants", [])]

    def test_excluded_from_the_budget_only_fallback(self, db, conversation):
        """"1000 mein kya milega" lists every open restaurant — an empty one must
        not be estimated against."""
        stub = self._stub(db)
        result = tools.find_restaurants(db, conversation, budget=5000)
        assert stub.name not in [r["name"] for r in result.get("restaurants", [])]

    def test_its_cuisine_is_not_offered_as_an_alternative(self, db, conversation):
        """A zero-match reply offers available_cuisines. Offering a cuisine with
        nothing orderable behind it is a second dead end straight after the first."""
        self._stub(db, name="Empty Kitchen", cuisine="Afghani")
        assert "Afghani" not in tools._available_cuisines(db)

    def test_a_fully_unavailable_menu_counts_as_no_menu(self, db, conversation, pizza):
        """Distinct from zero rows: the items exist but every one is switched off,
        so there is still nothing a customer could order."""
        db.execute(
            update(MenuItem)
            .where(MenuItem.restaurant_id == pizza.id)
            .values(is_available=False)
        )
        db.flush()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert "Pizza Junction" not in names

    def test_one_available_item_is_enough_to_be_offered(self, db, conversation, pizza):
        """The floor is exactly one orderable item — not a full menu."""
        db.execute(
            update(MenuItem)
            .where(MenuItem.restaurant_id == pizza.id)
            .values(is_available=False)
        )
        db.flush()
        first = db.scalars(
            select(MenuItem).where(MenuItem.restaurant_id == pizza.id).limit(1)
        ).one()
        first.is_available = True
        db.flush()

        names = [r["name"] for r in tools.list_restaurants(db, conversation)["restaurants"]]
        assert "Pizza Junction" in names
