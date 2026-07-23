"""Discovery-phase continuity — the shortlist the customer is choosing from.

Phase 8 anchored the conversation once a restaurant was ACTIVE. This covers the
step before that, which a fresh WhatsApp trace showed was still resetting:

    customer: "Biryani hai?"
    bot:      "Biryani serving restaurants: 1. Karachi Biryani House 2. Mandi House"
    customer: "Mandi house per hoti h biryani?"
    bot:      "Available restaurants: Wok & Roll, Pizza Junction"   <-- reset
    customer: "Biryani chaiye?"
    bot:      same generic reset
    customer: "Biryani ka batao"
    bot:      same generic reset

Two independent defects produced that, and both are pinned here:

  1. `find_matching_restaurants` matched the query as ONE substring, so any
     filler word ("chaiye", "ka batao", "hai") made it return zero — and the
     empty-result note then told the model to call list_restaurants. A dish
     word wrapped in ordinary Roman Urdu was enough to wipe the conversation.

  2. Nothing remembered the shortlist. `get_menu` has always recorded what it
     showed; the discovery tools recorded nothing, so a follow-up naming one of
     the just-offered restaurants could only be re-searched from scratch.
"""

from app.services import agent
from app.services import discovery as discovery_service
from app.services import tools


# --------------------------------------------------------------------------- #
# Defect 1 — filler words must not zero out a search
# --------------------------------------------------------------------------- #


class TestFillerWordQueries:
    """Every phrasing below came from the real trace and returned zero
    restaurants before the token fallback existed."""

    def test_trace_phrasings_all_find_the_biryani_restaurant(
        self, db, conversation, biryani,
    ):
        for query in (
            "Biryani chaiye",
            "Biryani chaiye?",
            "Biryani ka batao",
            "biryani hai",
            "mujhe biryani chahiye",
            "biryani hai kya",
        ):
            restaurants, _ = discovery_service.find_matching_restaurants(db, query)
            assert biryani.id in restaurants, f"{query!r} found nothing"

    def test_exact_phrase_still_wins_over_token_fallback(self, db, pizza, biryani):
        """Precision guard. "chicken biryani" matches as a whole phrase, so the
        broader per-token pass must NOT run — otherwise the loose "chicken"
        token would drag in Chicken Tikka Pizza and Chicken Chowmein and the
        customer's specific request would come back as "here's everything"."""
        restaurants, _ = discovery_service.find_matching_restaurants(
            db, "chicken biryani",
        )
        assert biryani.id in restaurants
        assert pizza.id not in restaurants

    def test_pure_filler_query_still_returns_nothing(self, db):
        """"kya hai" carries no dish signal. Stripping filler must leave zero
        terms and return empty — not fall through to matching everything."""
        restaurants, _ = discovery_service.find_matching_restaurants(db, "kya hai")
        assert restaurants == {}

    def test_significant_tokens_strips_filler_and_punctuation(self):
        assert discovery_service._significant_tokens("Biryani chaiye?") == ["biryani"]
        assert discovery_service._significant_tokens("Biryani ka batao") == ["biryani"]
        assert discovery_service._significant_tokens("kya hai") == []
        # Short tokens never survive — "h" as a LIKE term matches half the catalog.
        assert discovery_service._significant_tokens("biryani h") == ["biryani"]

    def test_tool_level_filler_query_returns_restaurants(
        self, db, conversation, biryani,
    ):
        result = tools.find_restaurants(db, conversation, query="Biryani ka batao")
        assert any(r["id"] == biryani.id for r in result["restaurants"])


# --------------------------------------------------------------------------- #
# Defect 2 — remember the shortlist
# --------------------------------------------------------------------------- #


class TestShownRestaurantMemory:

    def test_find_restaurants_records_what_it_showed(
        self, db, conversation, biryani,
    ):
        result = tools.find_restaurants(db, conversation, query="biryani")

        remembered = tools.shown_restaurants(conversation)
        assert [entry["id"] for entry in remembered] == [
            r["id"] for r in result["restaurants"]
        ]
        assert biryani.id in [entry["id"] for entry in remembered]
        assert conversation.context["shown_restaurants_query"] == "biryani"

    def test_list_restaurants_records_what_it_showed(self, db, conversation):
        tools.list_restaurants(db, conversation)
        names = {entry["name"] for entry in tools.shown_restaurants(conversation)}
        assert {"Karachi Biryani House", "Pizza Junction", "Wok & Roll"} <= names

    def test_empty_result_does_not_wipe_the_remembered_shortlist(
        self, db, conversation, biryani,
    ):
        """A dead-end search must leave the customer standing where they were."""
        tools.find_restaurants(db, conversation, query="biryani")
        tools.find_restaurants(db, conversation, query="sushi-omakase-nowhere-xyz")
        assert biryani.id in [e["id"] for e in tools.shown_restaurants(conversation)]


# --------------------------------------------------------------------------- #
# Naming a shortlisted restaurant is a SELECTION
# --------------------------------------------------------------------------- #


class TestShortlistSelection:

    def test_full_name_inside_a_question_is_a_selection(
        self, db, conversation, biryani,
    ):
        """The exact trace shape: shortlist, then "<name> per hoti h biryani?".
        Must resolve to that restaurant and show its menu, not re-search."""
        tools.find_restaurants(db, conversation, query="biryani")

        result = tools.find_restaurants(
            db, conversation, query="karachi biryani house per hoti h biryani?",
        )

        assert result["selected_from_shown_list"] is True
        assert result["restaurant"]["id"] == biryani.id
        assert result["items"]
        # get_menu ran for real — active restaurant set, grounding populated, so
        # Phase 8's restaurant-scoped continuity now carries the conversation.
        assert conversation.active_restaurant_id == biryani.id
        assert conversation.context["shown_menu_ids"]
        assert conversation.context["shown_menu_restaurant"] == biryani.name

    def test_distinctive_core_without_generic_word_selects(
        self, db, conversation, biryani,
    ):
        """"karachi biryani" identifies "Karachi Biryani House" — customers
        rarely type a restaurant's full legal name."""
        tools.find_restaurants(db, conversation, query="biryani")
        result = tools.find_restaurants(
            db, conversation, query="karachi biryani se order karna hai",
        )
        assert result.get("selected_from_shown_list") is True
        assert result["restaurant"]["id"] == biryani.id

    def test_dish_word_alone_is_not_a_selection(self, db, conversation, biryani):
        """"biryani chaiye" after a biryani shortlist is a repeat of the SEARCH,
        not a pick of "Karachi Biryani House" — resolving it as a selection
        would silently choose a restaurant on the customer's behalf."""
        tools.find_restaurants(db, conversation, query="biryani")
        result = tools.find_restaurants(db, conversation, query="biryani chaiye")

        assert "selected_from_shown_list" not in result
        assert conversation.active_restaurant_id is None
        # ...and it still returns the shortlist rather than nothing.
        assert any(r["id"] == biryani.id for r in result["restaurants"])

    def test_selection_only_considers_the_shown_list(self, db, conversation, pizza):
        """Naming a restaurant that was never offered is ordinary discovery.
        Scoping to the shortlist is what keeps this from becoming the silent
        restaurant-switch bug Phase 8 fixed."""
        result = tools.find_restaurants(db, conversation, query="Pizza Junction")

        assert "selected_from_shown_list" not in result
        assert conversation.active_restaurant_id is None
        assert any(r["id"] == pizza.id for r in result["restaurants"])

    def test_budget_query_naming_a_restaurant_is_not_a_selection(
        self, db, conversation, biryani,
    ):
        """"X mein 1500 mein kya milega" wants the budget comparison, and the
        selection path would swallow the estimate entirely."""
        tools.find_restaurants(db, conversation, query="biryani")
        result = tools.find_restaurants(
            db,
            conversation,
            query="karachi biryani house mein kya milega",
            budget=1500,
            party_size=2,
        )
        assert "selected_from_shown_list" not in result
        assert result["budget"] == "1500.00"

    def test_resolve_shown_candidate_is_punctuation_insensitive(
        self, db, conversation, biryani,
    ):
        tools.find_restaurants(db, conversation, query="biryani")
        for phrasing in (
            "Karachi Biryani House?",
            "karachi biryani house!!",
            "KARACHI  BIRYANI  HOUSE",
        ):
            assert tools.resolve_shown_candidate(conversation, phrasing) == biryani.id

    def test_no_shortlist_means_no_resolution(self, db, conversation):
        assert tools.resolve_shown_candidate(conversation, "Pizza Junction") is None


class TestSelectionAmbiguityFallsThrough:
    """Picking a restaurant for the customer is worse than asking one more
    question, so anything that could mean two candidates must NOT resolve —
    it falls through to the normal search and the model re-offers the list."""

    def test_two_different_names_in_one_message_do_not_resolve(
        self, db, conversation, biryani, pizza,
    ):
        """"X ya Y?" is a comparison, not a pick. Resolving it would silently
        choose one and show its menu as though the customer had decided."""
        tools.find_restaurants(db, conversation, query="chicken")
        shortlist = {e["name"] for e in tools.shown_restaurants(conversation)}
        assert {biryani.name, pizza.name} <= shortlist

        assert tools.resolve_shown_candidate(
            conversation, "karachi biryani house ya pizza junction?",
        ) is None

    def test_comparison_message_returns_a_list_not_a_menu(
        self, db, conversation, biryani, pizza,
    ):
        """Tool level: the same message must come back as candidates to choose
        between, with no restaurant silently made active."""
        tools.find_restaurants(db, conversation, query="chicken")
        result = tools.find_restaurants(
            db, conversation, query="karachi biryani house ya pizza junction",
        )

        assert "selected_from_shown_list" not in result
        assert conversation.active_restaurant_id is None
        ids = {r["id"] for r in result["restaurants"]}
        assert {biryani.id, pizza.id} <= ids

    def test_nested_names_resolve_to_the_longer_one(self, db, conversation):
        """The one case where multiple full-name hits are NOT ambiguous: one
        name contains the other, so it is a single mention. "Karachi Biryani
        House" must win over the "Biryani House" substring inside it."""
        conversation.context = {
            tools.SHOWN_RESTAURANTS_KEY: [
                {"id": 101, "name": "Biryani House"},
                {"id": 102, "name": "Karachi Biryani House"},
            ],
        }
        assert tools.resolve_shown_candidate(
            conversation, "karachi biryani house se order karna hai",
        ) == 102

    def test_shared_distinctive_core_does_not_resolve(self, db, conversation):
        """Tier B ambiguity. Two shortlisted restaurants whose cores are both
        "biryani" — the word identifies neither, so no selection."""
        conversation.context = {
            tools.SHOWN_RESTAURANTS_KEY: [
                {"id": 201, "name": "Biryani House"},
                {"id": 202, "name": "Biryani Kitchen"},
            ],
        }
        assert tools.resolve_shown_candidate(conversation, "biryani chaiye") is None

    def test_unique_distinctive_core_still_resolves(self, db, conversation):
        """Control for the test above — with only one "biryani"-cored
        candidate on the shortlist, the same word DOES identify it."""
        conversation.context = {
            tools.SHOWN_RESTAURANTS_KEY: [
                {"id": 201, "name": "Biryani House"},
                {"id": 202, "name": "Pizza Junction"},
            ],
        }
        assert tools.resolve_shown_candidate(conversation, "biryani chaiye") == 201


# --------------------------------------------------------------------------- #
# The empty-result note must stop commanding a reset
# --------------------------------------------------------------------------- #


class TestEmptyResultNote:

    def test_first_contact_still_recommends_list_restaurants(self, db, conversation):
        """Unchanged for a customer who has been shown nothing — offering the
        full list IS the right move on first contact."""
        result = tools.find_restaurants(
            db, conversation, query="sushi-omakase-nowhere-xyz",
        )
        assert "list_restaurants" in result["note"]

    def test_note_forbids_reset_once_a_shortlist_was_shown(
        self, db, conversation, biryani,
    ):
        tools.find_restaurants(db, conversation, query="biryani")
        result = tools.find_restaurants(
            db, conversation, query="sushi-omakase-nowhere-xyz",
        )
        assert result["restaurants"] == []
        assert "Do NOT call list_restaurants" in result["note"]
        # Names the shortlist so the model can re-offer it verbatim.
        assert biryani.name in result["note"]

    def test_note_forbids_reset_once_a_restaurant_is_active(
        self, db, conversation, pizza,
    ):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        result = tools.find_restaurants(
            db, conversation, query="sushi-omakase-nowhere-xyz",
        )
        assert "do NOT call list_restaurants" in result["note"]
        assert pizza.name in result["note"]


# --------------------------------------------------------------------------- #
# What the model actually sees in its system message
# --------------------------------------------------------------------------- #


class TestShownRestaurantFacts:

    def test_absent_before_anything_is_shown(self, db, conversation):
        assert agent._shown_restaurant_facts(conversation) == ""

    def test_lists_the_shortlist_and_forbids_a_generic_reset(
        self, db, conversation, biryani,
    ):
        tools.find_restaurants(db, conversation, query="biryani")
        facts = agent._shown_restaurant_facts(conversation)

        assert biryani.name in facts
        assert f"id={biryani.id}" in facts
        assert "SELECTION" in facts
        assert "do NOT call list_restaurants" in facts

    def test_yields_to_the_active_restaurant_anchor(
        self, db, conversation, pizza,
    ):
        """Once a restaurant is picked, `_active_restaurant_facts` is the
        stronger anchor — two competing "focus on this" directives in one
        system message only muddy the turn."""
        tools.find_restaurants(db, conversation, query="pizza")
        assert agent._shown_restaurant_facts(conversation) != ""

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        assert agent._shown_restaurant_facts(conversation) == ""

    def test_reaches_the_built_system_message(self, db, conversation, biryani):
        tools.find_restaurants(db, conversation, query="biryani")
        messages = agent._build_messages(db, conversation)
        facts = messages[1]["content"]
        assert "ALREADY shown this customer" in facts
        assert biryani.name in facts
