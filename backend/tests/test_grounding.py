"""The grounding auditor — services/grounding.py.

The three guards it replaces keep their original tests in test_agent_guards.py
(re-pointed at `audit`), because each of those encodes a real production failure
and must keep biting. THIS file covers what the auditor can do that none of them
could.
"""

from decimal import Decimal

from app.services import grounding as G


def _listing(*names, tool="find_restaurants"):
    return [{
        "tool": tool, "args": "{}",
        "result": {"restaurants": [{"id": i, "name": n} for i, n in enumerate(names, 1)]},
    }]


class TestUndercountIsFixed:
    """The old `_ungrounded_restaurant_list` COUNTED recognised names and fired at
    two or more, so a list mixing one real restaurant with one invented one counted
    only one and escaped — the more fabricated the list, the less likely it was
    caught. The auditor identifies the list from any recognised entry, then requires
    EVERY entry to be grounded."""

    MIXED = "1. Karachi Biryani House\n2. Lahore Tikka House"

    def test_a_list_mixing_a_real_and_an_invented_restaurant_is_caught(
        self, db, conversation,
    ):
        violation = G.audit(db, conversation, self.MIXED, _listing("Karachi Biryani House"))
        assert violation is not None
        assert violation.kind == "unlisted_offer"
        assert "Lahore Tikka House" in violation.nudge

    def test_the_same_list_passes_when_the_tool_returned_both(self, db, conversation):
        trace = _listing("Karachi Biryani House", "Lahore Tikka House")
        assert G.audit(db, conversation, self.MIXED, trace) is None

    def test_a_single_numbered_restaurant_is_not_an_offer(self, db, conversation):
        """Two entries make it a list of CHOICES; one is a confirmation."""
        reply = "Aapka order confirm hai:\n1. Pizza Junction — Chicken Tikka Pizza"
        assert G.audit(db, conversation, reply, [], check_prices=False) is None


class TestPriceGrounding:
    """Tier 2. No old guard checked arbitrary figures — only a quoted Total and the
    two-label bill shape were policed, so an invented price in prose sailed through."""

    def test_an_invented_price_is_caught(self, db, conversation):
        violation = G.audit(db, conversation, "Chicken Biryani Rs. 999 ki hai.", [])
        assert violation is not None
        assert violation.kind == "invented_amount"

    def test_a_real_menu_price_passes(self, db, conversation):
        assert G.audit(db, conversation, "Chicken Biryani Rs. 450 ki hai.", []) is None

    def test_a_restaurant_delivery_fee_passes(self, db, conversation, pizza):
        reply = f"Delivery Rs. {pizza.delivery_fee:.0f} hai."
        assert G.audit(db, conversation, reply, []) is None

    def test_a_figure_the_customer_typed_passes(self, db, conversation):
        """Echoing the customer's own budget back is not a fabrication."""
        reply = "Rs. 1500 mein aap ye le sakte hain."
        assert G.audit(db, conversation, reply, [],
                       customer_figures={Decimal("1500")}) is None
        # ...and without that allowance it is correctly flagged.
        violation = G.audit(db, conversation, reply, [])
        assert violation is not None and violation.kind == "invented_amount"

    def test_preview_bill_components_pass(self, db, conversation):
        trace = [{"tool": "preview_bill", "args": "{}", "result": {
            "subtotal": "1150.00", "tax_amount": "92.00", "delivery_fee": "100.00",
            "discount_amount": "0.00", "total": "1342.00",
        }}]
        reply = ("Subtotal: Rs. 1150.00\nTax: Rs. 92.00\nDelivery: Rs. 100.00\n"
                 "Total: Rs. 1342.00")
        assert G.audit(db, conversation, reply, trace) is None

    def test_price_checking_can_be_switched_off(self, db, conversation):
        assert G.audit(db, conversation, "Rs. 999", [], check_prices=False) is None


class TestSearchFidelityInProse:
    """The list rule only sees NUMBERED lists. Padding can also arrive as prose —
    "X hai, aur Y bhi acha hai" — which is what this rule uniquely covers. Mutation
    testing caught that nothing exercised it: every other padding test uses a
    numbered list, so rule 1 fired first and this one was never reached."""

    def test_a_stray_restaurant_named_in_prose_is_caught(self, db, conversation):
        reply = "Biryani ke liye Karachi Biryani House hai, aur Pizza Junction bhi acha hai."
        violation = G.audit(db, conversation, reply,
                            _listing("Karachi Biryani House"), check_prices=False)
        assert violation is not None
        assert violation.kind == "search_padding"
        assert "Pizza Junction" in violation.nudge

    def test_prose_naming_only_returned_restaurants_passes(self, db, conversation):
        reply = "Biryani ke liye Karachi Biryani House hai. Menu dikhaun?"
        assert G.audit(db, conversation, reply, _listing("Karachi Biryani House"),
                       check_prices=False) is None


class TestRuleOrdering:
    """The first violation wins, so the nudge names the real problem."""

    def test_an_ungrounded_list_is_reported_before_a_price_problem(
        self, db, conversation,
    ):
        reply = "1. Karachi Biryani House — Rs. 999\n2. Pizza Junction — Rs. 888"
        violation = G.audit(db, conversation, reply, [])
        assert violation is not None and violation.kind == "unlisted_offer"


class TestForceTool:
    """A missing tool call must FORCE one; a wrong-content reply only needs a
    rewrite. The auditor carries that distinction so the agent stays simple."""

    def test_no_listing_tool_forces_one(self, db, conversation):
        v = G.audit(db, conversation, "1. Karachi Biryani House\n2. Pizza Junction", [])
        assert v.force_tool is True

    def test_a_padded_list_only_needs_a_rewrite(self, db, conversation):
        v = G.audit(db, conversation, "1. Karachi Biryani House\n2. Pizza Junction",
                    _listing("Karachi Biryani House"))
        assert v.force_tool is False

    def test_an_unbacked_bill_forces_a_preview(self, db, conversation):
        reply = "Subtotal Rs. 1150\nTax: Rs. 0\nDelivery: Rs. 0\nTotal: Rs. 1150"
        v = G.audit(db, conversation, reply, [])
        assert v.kind == "unbacked_bill" and v.force_tool is True
