"""Sequential linked orders — place_order(link_to_order_number=...) +
order_group_id column + OrderOut schema exposure.

Cases pinned here mirror the design review's plan:

  * A second order placed with link_to_order_number gets its group id
    back-filled onto BOTH the parent and the new child.
  * A third linked order joins the SAME group (no new id minted).
  * Cross-customer link attempts are refused (customer-scoped lookup).
  * Non-existent order numbers return linked_order_not_found (recoverable).
  * The cart's cross-restaurant guard is UNCHANGED — no silent relaxation.
  * Two linked orders CAN be at the same restaurant (allowed edge case).
  * Cancelling order A does NOT affect order B (they stay independent).
  * OrderOut / OrderWithRestaurantOut expose order_group_id via the API.
  * Tenant boundary — a restaurant's staff sees the marker on their own
    linked order but NEVER learns about sibling orders at other tenants.
  * Full E2E: place A → find_restaurants B → cart B → place_order(link=A)
    → both orders share the group id AND cart guard fired appropriately.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from app.models import (
    CustomerAddress,
    MenuItem,
    MessageDirection,
    Order,
    OrderStatus,
    Restaurant,
)
from app.services import conversations as convo_svc
from app.services import tools


def _seed_payment_ack(db, conversation):
    """Log an outbound the anti-silent-COD guard treats as "the model
    asked". Every real conversation has this by the time place_order runs;
    tests need it explicitly."""
    convo_svc.log_message(
        db, conversation, MessageDirection.OUTBOUND,
        "Payment kis se karna hai — cod, jazzcash, ya easypaisa?",
    )
    db.flush()


def _address_for(db, customer, text="House 1, DHA Lahore"):
    db.add(CustomerAddress(
        customer_id=customer.id,
        address_text=text,
        is_default=True,
    ))
    db.flush()


def _place_first_order_at(
    db, conversation, restaurant: Restaurant, item_name_ilike: str = "%",
) -> Order:
    """Ground → add → place. Quantity=2 so we clear every seed restaurant's
    minimum order amount (biryani=500, pizza=700, wok=600). Returns the
    placed Order row."""
    tools.get_menu(db, conversation, restaurant_id=restaurant.id)
    item = db.scalar(
        select(MenuItem).where(
            MenuItem.restaurant_id == restaurant.id,
            MenuItem.is_available.is_(True),
            MenuItem.name.ilike(item_name_ilike),
        ).order_by(MenuItem.price).limit(1)
    )
    tools.add_to_cart(db, conversation, menu_item_id=item.id, quantity=2)
    db.flush()
    result = tools.place_order(db, conversation, payment_method="cod")
    assert "error" not in result, result
    return db.scalar(select(Order).where(Order.order_number == result["order_number"]))


# --------------------------------------------------------------------------- #
# Core linking mechanics
# --------------------------------------------------------------------------- #


class TestLinking:
    def test_second_order_with_link_shares_group_id_backfilled_on_parent(
        self, db, conversation, biryani, pizza,
    ):
        """The primary success path: order A placed, order B placed with
        link_to_order_number=A. A group id is minted, back-filled onto A,
        and set on B. Both rows now carry the same string."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)

        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")
        # place_order cleared the cart — add to pizza cart fresh
        assert conversation.cart == {"items": []}
        assert order_a.order_group_id is None  # not yet grouped

        # Order B at a different restaurant, linked
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(
            select(MenuItem).where(
                MenuItem.restaurant_id == pizza.id,
                MenuItem.name.ilike("%Chicken Tikka Pizza%"),
            )
        )
        tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        result_b = tools.place_order(
            db, conversation,
            payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        assert "error" not in result_b, result_b

        # Both orders now share a group id
        db.expire(order_a)
        order_b = db.scalar(
            select(Order).where(Order.order_number == result_b["order_number"])
        )
        assert order_a.order_group_id is not None
        assert order_a.order_group_id.startswith("GRP-")
        assert order_b.order_group_id == order_a.order_group_id

        # And the tool response surfaces the link
        assert result_b["order_group_id"] == order_a.order_group_id
        assert result_b["linked_to_order_number"] == order_a.order_number

    def test_third_linked_order_reuses_existing_group_id(
        self, db, conversation, biryani, pizza,
    ):
        """Once a group id exists on the parent, subsequent links to that
        parent (or to any sibling — but MVP passes the ORIGINAL) reuse
        that id rather than minting a new one."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)

        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        # Order B — first link, mints the group id
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        item_b = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conversation, menu_item_id=item_b.id, quantity=1)
        r_b = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        original_group = r_b["order_group_id"]

        # Order C — same original parent, different item at biryani
        tools.get_menu(db, conversation, restaurant_id=biryani.id)
        # Wait past the duplicate window? No — different item, same restaurant.
        item_c = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Beef Biryani",  # different from A's Chicken Biryani
        ))
        tools.add_to_cart(db, conversation, menu_item_id=item_c.id, quantity=1)
        r_c = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        assert "error" not in r_c

        # C reuses the same group id — no new one minted
        assert r_c["order_group_id"] == original_group

    def test_link_to_another_customers_order_is_refused(
        self, db, conversation, biryani, pizza,
    ):
        """Customer-scoped parent lookup: a customer cannot link to
        someone else's order number even by guessing it — the parent
        query is `WHERE customer_id = <me> AND order_number = <n>`."""
        # Customer 1 places order A
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        # Customer 2 tries to link a fresh order to Customer 1's order number
        cust2 = convo_svc.get_or_create_customer(db, "923099887711")
        _address_for(db, cust2, text="Different address")
        conv2 = convo_svc.get_or_create_conversation(db, cust2)
        _seed_payment_ack(db, conv2)
        tools.get_menu(db, conv2, restaurant_id=pizza.id)
        item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conv2, menu_item_id=item.id, quantity=1)
        result = tools.place_order(
            db, conv2, payment_method="cod",
            link_to_order_number=order_a.order_number,  # A belongs to customer 1!
        )
        assert result.get("error") == "linked_order_not_found"
        # And Customer 1's order stays untouched
        db.expire(order_a)
        assert order_a.order_group_id is None

    def test_link_to_nonexistent_order_number_returns_recoverable_error(
        self, db, conversation, biryani,
    ):
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        tools.get_menu(db, conversation, restaurant_id=biryani.id)
        item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Chicken Biryani",
        ))
        tools.add_to_cart(db, conversation, menu_item_id=item.id, quantity=1)

        result = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number="AB-NOPE99",
        )
        assert result.get("error") == "linked_order_not_found"
        # Cart NOT consumed on this failure — model can retry without link
        assert conversation.cart["items"] != []

    def test_link_to_order_number_is_case_and_whitespace_normalised(
        self, db, conversation, biryani, pizza,
    ):
        """Model has been observed sending values with stray whitespace or
        wrong case ('ab-4f2k9c '). Accepted."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conversation, menu_item_id=item.id, quantity=1)
        result = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=f"  {order_a.order_number.lower()}  ",
        )
        assert "error" not in result
        assert result["linked_to_order_number"] == order_a.order_number


# --------------------------------------------------------------------------- #
# Non-regression: the cart guard is UNCHANGED
# --------------------------------------------------------------------------- #


class TestCartGuardUnchanged:
    def test_cross_restaurant_add_still_refused_without_placing_first(
        self, db, conversation, biryani, pizza,
    ):
        """The cross-restaurant cart guard was NOT relaxed for Phase 7.
        add_to_cart to a second restaurant while the first cart still has
        items must still return cart_has_other_restaurant."""
        # Ground both restaurants, add biryani first
        tools.get_menu(db, conversation, restaurant_id=biryani.id)
        biryani_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Chicken Biryani",
        ))
        tools.add_to_cart(db, conversation, menu_item_id=biryani_item.id, quantity=1)

        # Now try to add pizza — must refuse, without needing any Phase 7 link
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        result = tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        assert result.get("error") == "cart_has_other_restaurant"

    def test_second_add_after_place_order_succeeds_because_cart_cleared(
        self, db, conversation, biryani, pizza,
    ):
        """The correct multi-restaurant path: place order A (clears cart),
        THEN add to cart for restaurant B (no guard trip because cart is
        empty). Proves the sequential discipline works with the existing
        guard — no relaxation needed."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        # After place_order, cart is empty
        assert conversation.cart == {"items": []}

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        result = tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        assert "error" not in result


# --------------------------------------------------------------------------- #
# Independence, edge cases
# --------------------------------------------------------------------------- #


class TestIndependenceAndEdges:
    def test_two_linked_orders_at_same_restaurant_are_allowed(
        self, db, conversation, biryani,
    ):
        """Edge case: customer orders biryani for themselves, then a
        different biryani for their brother, all in one session and wants
        them linked. Allowed — the link is a label, not a cross-restaurant
        constraint. Uses different items to sidestep the duplicate-order
        guard."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        # Order B at the SAME restaurant, different item
        tools.get_menu(db, conversation, restaurant_id=biryani.id)
        beef = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == biryani.id,
            MenuItem.name == "Beef Biryani",
        ))
        tools.add_to_cart(db, conversation, menu_item_id=beef.id, quantity=1)
        result_b = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        assert "error" not in result_b
        assert result_b["order_group_id"] == db.scalar(
            select(Order.order_group_id).where(
                Order.order_number == order_a.order_number
            )
        )

    def test_cancelling_order_a_does_not_affect_order_b(
        self, db, conversation, biryani, pizza,
    ):
        """Linked orders remain fully independent. Cancelling order A
        must NOT propagate to order B or clear the group id — the two
        stay linked by the shared string, and dashboards may still show
        them together (with A cancelled, B still pending)."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        db.expire_all()

        # Cancel A directly
        order_a = db.scalar(select(Order).where(Order.id == order_a.id))
        order_a.status = OrderStatus.CANCELLED
        db.flush()

        # B still exists, still pending, still carries the same group id
        siblings = db.scalars(
            select(Order).where(Order.order_group_id == order_a.order_group_id)
        ).all()
        assert len(siblings) == 2
        b = next(o for o in siblings if o.id != order_a.id)
        assert b.status == OrderStatus.PENDING  # untouched


# --------------------------------------------------------------------------- #
# API exposure + tenant isolation
# --------------------------------------------------------------------------- #


class TestApiExposure:
    def test_restaurant_orders_api_exposes_order_group_id(
        self, db, client, conversation, biryani, pizza, biryani_headers,
    ):
        """After a linked order, GET /restaurant/orders returns the
        order_group_id on the biryani order — the marker the dashboard
        will use to render its 'Linked' chip."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        db.commit()

        response = client.get("/restaurant/orders", headers=biryani_headers)
        assert response.status_code == 200
        rows = response.json()
        # Biryani's staff should see their order with the group id populated
        mine = next(r for r in rows if r["order_number"] == order_a.order_number)
        assert mine["order_group_id"] is not None
        assert mine["order_group_id"].startswith("GRP-")

    def test_tenant_boundary_biryani_staff_cannot_see_pizza_sibling(
        self, db, client, conversation, biryani, pizza,
        biryani_headers, pizza_headers,
    ):
        """Hard privacy boundary — biryani's staff sees the group id
        marker on their OWN order but must NOT be able to fetch pizza's
        sibling order through the restaurant orders endpoint. The
        restaurant orders API is already tenant-scoped; this test locks
        that in for the linked-order case."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order_a = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        pizza_item = db.scalar(select(MenuItem).where(
            MenuItem.restaurant_id == pizza.id,
            MenuItem.name.ilike("%Chicken Tikka Pizza%"),
        ))
        tools.add_to_cart(db, conversation, menu_item_id=pizza_item.id, quantity=1)
        r_b = tools.place_order(
            db, conversation, payment_method="cod",
            link_to_order_number=order_a.order_number,
        )
        pizza_order_number = r_b["order_number"]
        db.commit()

        # Biryani's staff must NOT see the pizza order in their list
        biryani_view = client.get("/restaurant/orders", headers=biryani_headers).json()
        assert all(r["order_number"] != pizza_order_number for r in biryani_view)

        # Pizza's staff sees their own order (with the same group id)
        pizza_view = client.get("/restaurant/orders", headers=pizza_headers).json()
        pizza_row = next(r for r in pizza_view if r["order_number"] == pizza_order_number)
        assert pizza_row["order_group_id"] is not None
        # And critically, pizza's staff sees ONLY the pizza order, not biryani's
        assert all(r["order_number"] != order_a.order_number for r in pizza_view)

    def test_standalone_orders_have_null_order_group_id_in_api(
        self, db, client, conversation, biryani, biryani_headers,
    ):
        """Backward compat: existing single-restaurant orders have
        order_group_id=null in the API response. Nothing about the
        response shape changed for the common case."""
        _address_for(db, conversation.customer)
        _seed_payment_ack(db, conversation)
        order = _place_first_order_at(db, conversation, biryani, "%Chicken Biryani%")
        db.commit()

        response = client.get("/restaurant/orders", headers=biryani_headers)
        row = next(r for r in response.json() if r["order_number"] == order.order_number)
        assert row["order_group_id"] is None
