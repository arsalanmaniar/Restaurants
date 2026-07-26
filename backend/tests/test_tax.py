"""Phase F — sales tax on the bill, and the payment-method-dependent total.

Confirmed business rules (2026-07-24):
  * Tax rate depends on payment method: 15% cash-on-delivery, 8% online.
  * Tax is on the food subtotal NET of any coupon discount, never on delivery.
  * Commission base is subtotal + tax (delivery excluded), then − discount.
  * Payment method is chosen BEFORE the read-back, so the read-back total is
    correct; preview_bill produces that read-back total without placing anything.

The highest-risk property, pinned hard: tax lands in total_amount BEFORE the
Payment row is snapshotted, so an online payment link is for the taxed total and
the callback's amount check passes.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import MessageDirection, Order, PaymentAttemptStatus, PaymentMethod
from app.services import billing
from app.services import conversations as convo_svc
from app.services import tools


def _seed_outbound(db, conversation):
    """Log an outbound so the conversation looks like a real, model-driven flow.
    The Bug 1 preview guard (like the silent-COD guard) only fires once the model
    has actually been talking — direct-tool/seed callers with no outbound skip it.
    """
    convo_svc.log_message(
        db, conversation, MessageDirection.OUTBOUND,
        "Payment kis se karna hai — cod, jazzcash, ya easypaisa?",
    )
    db.flush()


def _preview(db, conversation, **kwargs):
    """preview_bill carrying the delivery details the Issue 3 one-bill gate requires
    in an outbound-seeded (real-looking) flow. These tests are about the preview →
    place_order handshake, not about the delivery gate, so they supply them once here
    instead of repeating them at every call site."""
    kwargs.setdefault("delivery_address", "House 1")
    kwargs.setdefault("contact_name", "Test Customer")
    return tools.preview_bill(db, conversation, **kwargs)


# --------------------------------------------------------------------------- #
# Pure arithmetic — services/billing.py
# --------------------------------------------------------------------------- #


class TestComputeBill:

    def test_cod_is_taxed_15_percent(self):
        bill = billing.compute_bill(
            subtotal=Decimal("2300.00"),
            delivery_fee=Decimal("100.00"),
            discount=Decimal("0.00"),
            method=PaymentMethod.COD,
        )
        assert bill.tax_rate == Decimal("15")
        assert bill.tax_amount == Decimal("345.00")
        assert bill.total == Decimal("2745.00")  # 2300 + 345 + 100

    @pytest.mark.parametrize("method", [PaymentMethod.JAZZCASH, PaymentMethod.EASYPAISA])
    def test_online_is_taxed_8_percent(self, method):
        bill = billing.compute_bill(
            subtotal=Decimal("2300.00"),
            delivery_fee=Decimal("100.00"),
            discount=Decimal("0.00"),
            method=method,
        )
        assert bill.tax_rate == Decimal("8")
        assert bill.tax_amount == Decimal("184.00")
        assert bill.total == Decimal("2584.00")  # 2300 + 184 + 100

    def test_tax_is_on_food_only_not_delivery(self):
        """A bigger delivery fee must not change the tax."""
        a = billing.compute_bill(
            subtotal=Decimal("1000.00"), delivery_fee=Decimal("50.00"),
            discount=Decimal("0.00"), method=PaymentMethod.COD,
        )
        b = billing.compute_bill(
            subtotal=Decimal("1000.00"), delivery_fee=Decimal("500.00"),
            discount=Decimal("0.00"), method=PaymentMethod.COD,
        )
        assert a.tax_amount == b.tax_amount == Decimal("150.00")

    def test_tax_is_on_discounted_food(self):
        """Decision 2: tax on (subtotal − discount)."""
        bill = billing.compute_bill(
            subtotal=Decimal("2000.00"), delivery_fee=Decimal("100.00"),
            discount=Decimal("300.00"), method=PaymentMethod.COD,
        )
        # (2000 − 300) × 15% = 255
        assert bill.tax_amount == Decimal("255.00")
        # 2000 + 255 + 100 − 300
        assert bill.total == Decimal("2055.00")

    def test_discount_larger_than_food_never_makes_negative_tax(self):
        bill = billing.compute_bill(
            subtotal=Decimal("500.00"), delivery_fee=Decimal("100.00"),
            discount=Decimal("800.00"), method=PaymentMethod.COD,
        )
        assert bill.tax_amount == Decimal("0.00")


# --------------------------------------------------------------------------- #
# place_order stores tax and the method-dependent total
# --------------------------------------------------------------------------- #


class TestPlaceOrderTax:

    def _order(self, db, conversation, **kwargs):
        result = tools.place_order(db, conversation, delivery_address="House 1", **kwargs)
        assert "order_number" in result, result
        return result, db.scalar(
            select(Order).where(Order.order_number == result["order_number"])
        )

    def test_cod_order_stores_15_percent_tax(self, db, cart_with_pizza):
        result, order = self._order(db, cart_with_pizza, payment_method="cod")
        assert order.tax_rate == Decimal("15.00")
        assert order.tax_amount == (order.subtotal * Decimal("0.15")).quantize(Decimal("0.01"))
        assert order.total_amount == order.subtotal + order.tax_amount + order.delivery_fee
        assert result["tax_amount"] == f"{order.tax_amount:.2f}"

    def test_online_order_stores_8_percent_tax(self, db, cart_with_pizza):
        result, order = self._order(db, cart_with_pizza, payment_method="jazzcash")
        assert order.tax_rate == Decimal("8.00")
        assert order.tax_amount == (order.subtotal * Decimal("0.08")).quantize(Decimal("0.01"))

    def test_same_cart_costs_less_online_than_cod(self, db, conversation, pizza, menu_item):
        """The whole reason payment method comes first: the total differs. Two
        separate customers place the identical cart (a second identical order for
        the SAME customer would be caught by the duplicate-order guard)."""
        from app.services import conversations as convo

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        cod = tools.place_order(db, conversation, delivery_address="X", payment_method="cod")

        other_customer = convo.get_or_create_customer(db, "923009999999")
        other = convo.get_or_create_conversation(db, other_customer)
        tools.get_menu(db, other, restaurant_id=pizza.id)
        tools.add_to_cart(db, other, menu_item_id=menu_item.id, quantity=1)
        online = tools.place_order(db, other, delivery_address="X", payment_method="easypaisa")

        assert "duplicate_prevented" not in online
        assert Decimal(online["total"]) < Decimal(cod["total"])

    def test_commission_base_is_subtotal_plus_tax_excluding_delivery(
        self, db, cart_with_pizza, pizza,
    ):
        _, order = self._order(db, cart_with_pizza, payment_method="cod")
        expected = (
            (order.subtotal + order.tax_amount) * pizza.commission_rate / Decimal("100")
        ).quantize(Decimal("0.01"))
        assert order.commission_amount == expected


# --------------------------------------------------------------------------- #
# The amount-mismatch trap: online payment link is for the TAXED total
# --------------------------------------------------------------------------- #


class TestAmountMismatchTrap:

    def test_payment_amount_snapshots_the_taxed_total(self, db, cart_with_pizza):
        """start_payment records Payment.amount = order.total_amount. If tax were
        added AFTER the Payment row, the gateway would collect the wrong amount
        and every callback would fail the amount check. Prove they agree."""
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="House 1", payment_method="jazzcash"
        )
        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))

        # An online order has exactly one payment attempt, for the taxed total.
        assert len(order.payments) == 1
        payment = order.payments[0]
        assert payment.amount == order.total_amount
        assert order.tax_amount > 0  # tax really is in there
        # The link amount the customer will be charged == the stored taxed total.
        assert payment.status == PaymentAttemptStatus.INITIATED

    def test_reported_total_matches_stored_total(self, db, cart_with_pizza):
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="House 1", payment_method="easypaisa"
        )
        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
        assert result["total"] == f"{order.total_amount:.2f}"
        assert order.payments[0].amount == Decimal(result["total"])


# --------------------------------------------------------------------------- #
# preview_bill — read-back numbers, no mutation
# --------------------------------------------------------------------------- #


class TestPreviewBill:

    def test_preview_matches_what_place_order_charges(self, db, conversation, pizza, menu_item):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)

        preview = tools.preview_bill(db, conversation, payment_method="cod")
        placed = tools.place_order(db, conversation, delivery_address="X", payment_method="cod")

        for field in ("subtotal", "tax_amount", "delivery_fee", "total"):
            assert preview[field] == placed[field], field

    def test_preview_reflects_the_payment_method(self, db, cart_with_pizza):
        cod = tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        online = tools.preview_bill(db, cart_with_pizza, payment_method="jazzcash")
        assert Decimal(cod["tax_amount"]) > Decimal(online["tax_amount"])
        assert Decimal(cod["total"]) > Decimal(online["total"])

    def test_preview_places_nothing(self, db, cart_with_pizza):
        before = db.scalar(select(Order).where(Order.customer_id == cart_with_pizza.customer_id))
        tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        after = db.scalar(select(Order).where(Order.customer_id == cart_with_pizza.customer_id))
        assert before is None and after is None  # no order created
        assert cart_with_pizza.cart["items"], "cart must be untouched"

    def test_preview_empty_cart_is_an_error(self, db, conversation):
        assert "error" in tools.preview_bill(db, conversation)

    def test_preview_flags_below_minimum_without_erroring(
        self, db, conversation, pizza, menu_item,
    ):
        pizza.min_order_amount = Decimal("99999.00")
        db.flush()
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)

        preview = tools.preview_bill(db, conversation, payment_method="cod")
        assert "error" not in preview
        assert preview["below_minimum"] is True


# --------------------------------------------------------------------------- #
# Bug 1 — the bait-and-switch guard: no order commits unless the customer was
# shown the exact taxed bill for THIS cart + method + coupon via preview_bill.
# --------------------------------------------------------------------------- #


class TestPreviewGuard:

    def _cart(self, db, conversation, pizza, menu_item, qty=2):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=qty)
        db.flush()

    def test_real_flow_without_a_preview_is_refused_and_places_nothing(
        self, db, conversation, pizza, menu_item,
    ):
        """The core bug: in a live conversation the model tries to place the order
        without ever having shown the taxed bill. Refused, and no Order is created."""
        self._cart(db, conversation, pizza, menu_item)
        _seed_outbound(db, conversation)

        result = tools.place_order(
            db, conversation, delivery_address="House 1", payment_method="cod"
        )

        assert result.get("error") == "preview_first"
        assert db.scalar(
            select(Order).where(Order.customer_id == conversation.customer_id)
        ) is None
        assert conversation.cart["items"], "cart must be untouched so the model can retry"

    def test_place_succeeds_after_a_matching_preview(
        self, db, conversation, pizza, menu_item,
    ):
        self._cart(db, conversation, pizza, menu_item)
        _seed_outbound(db, conversation)

        _preview(db, conversation, payment_method="cod")
        result = tools.place_order(
            db, conversation, delivery_address="House 1", payment_method="cod",
            contact_name="Test Customer",
        )

        assert "order_number" in result, result

    def test_preview_for_one_method_does_not_authorise_another(
        self, db, conversation, pizza, menu_item,
    ):
        """The bait-and-switch by payment method: a preview shown for COD (15% tax)
        must NOT wave through a placement as jazzcash (8% tax) — that is a different
        total the customer never saw. The mismatch forces a fresh preview."""
        self._cart(db, conversation, pizza, menu_item)
        _seed_outbound(db, conversation)

        _preview(db, conversation, payment_method="cod")
        result = tools.place_order(
            db, conversation, delivery_address="House 1", payment_method="jazzcash"
        )

        assert result.get("error") == "preview_first"

    def test_changing_the_cart_after_preview_forces_a_new_preview(
        self, db, conversation, pizza, menu_item,
    ):
        self._cart(db, conversation, pizza, menu_item)
        _seed_outbound(db, conversation)
        _preview(db, conversation, payment_method="cod")

        # Customer adds another pizza — the previewed total is now stale.
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        result = tools.place_order(
            db, conversation, delivery_address="House 1", payment_method="cod"
        )

        assert result.get("error") == "preview_first"

    def test_successful_placement_clears_the_preview_marker(
        self, db, conversation, pizza, menu_item,
    ):
        self._cart(db, conversation, pizza, menu_item)
        _seed_outbound(db, conversation)
        _preview(db, conversation, payment_method="cod")
        tools.place_order(
            db, conversation, delivery_address="House 1", payment_method="cod",
            contact_name="Test Customer",
        )

        assert "previewed_bill" not in (conversation.context or {})

    def test_direct_tool_call_without_an_outbound_still_places(
        self, db, cart_with_pizza,
    ):
        """Non-regression: a direct-tool / seed caller (no outbound, never drives a
        read-back) is unaffected — place_order still works without a preview, the
        same escape hatch the silent-COD guard uses."""
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="House 1", payment_method="cod"
        )
        assert "order_number" in result, result

    def test_preview_bill_refuses_an_unavailable_method(
        self, db, cart_with_pizza, monkeypatch,
    ):
        """preview_bill validates availability exactly like place_order — so a method
        that placement would reject never gets a false read-back total (the AB-5ABBE2
        bait-and-switch). No total, and no previewed_bill stamp for the bad method."""
        monkeypatch.setattr(tools, "available_methods", lambda: [PaymentMethod.COD])
        result = tools.preview_bill(db, cart_with_pizza, payment_method="jazzcash")
        assert result["error"] == "unavailable_payment_method"
        assert "total" not in result
        assert "previewed_bill" not in (cart_with_pizza.context or {})


class TestOneBillGate:
    """Issue 3: the customer must see ONE bill, and only once EVERYTHING is decided.
    The reported conversation showed two — a fabricated Rs. 0-tax one before the
    payment method was chosen, then the real one before the address was collected.

    The delivery details change no number here (delivery fee is a flat per-restaurant
    charge); this gate is purely about ORDER OF EVENTS, made deterministic because
    prompt-only sequencing is what failed in AB-F6DF70."""

    def test_refuses_to_bill_before_an_address_is_known(self, db, cart_with_pizza):
        _seed_outbound(db, cart_with_pizza)
        result = tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        assert result["error"] == "missing_address"
        assert "total" not in result, "no bill may be produced before the address"
        assert "previewed_bill" not in (cart_with_pizza.context or {})

    def test_refuses_to_bill_before_a_contact_name_is_known(self, db, cart_with_pizza):
        _seed_outbound(db, cart_with_pizza)
        result = tools.preview_bill(
            db, cart_with_pizza, payment_method="cod", delivery_address="House 1, Lahore",
        )
        assert result["error"] == "missing_contact_name"
        assert "total" not in result

    def test_bills_once_everything_is_known(self, db, cart_with_pizza):
        _seed_outbound(db, cart_with_pizza)
        result = tools.preview_bill(
            db, cart_with_pizza, payment_method="cod",
            delivery_address="House 1, Lahore", contact_name="Ayesha",
        )
        assert "error" not in result, result
        assert result["total"] is not None
        # Echoed back so the read-back's Address/name lines come from ground truth.
        assert result["delivery_address"] == "House 1, Lahore"
        assert result["contact_name"] == "Ayesha"

    def test_a_saved_default_address_satisfies_the_gate(self, db, cart_with_pizza):
        """A returning customer is never re-asked for an address they already gave."""
        from app.models import CustomerAddress

        _seed_outbound(db, cart_with_pizza)
        db.add(CustomerAddress(
            customer_id=cart_with_pizza.customer_id,
            address_text="House 9, Gulberg, Lahore",
            is_default=True,
        ))
        cart_with_pizza.customer.name = "Bilal"
        db.flush()

        result = tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        assert "error" not in result, result
        assert result["delivery_address"] == "House 9, Gulberg, Lahore"

    def test_a_shared_pin_satisfies_the_gate_and_is_not_consumed(
        self, db, cart_with_pizza,
    ):
        """The subtle one: place_order CONSUMES the pin, so if preview_bill consumed it
        too the coordinates would be gone by placement and the order would lose them."""
        _seed_outbound(db, cart_with_pizza)
        cart_with_pizza.context = {
            **(cart_with_pizza.context or {}),
            "delivery_location": {"lat": 24.8607, "lng": 67.0011},
        }
        cart_with_pizza.customer.name = "Bilal"
        db.flush()

        result = tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        assert "error" not in result, result
        assert "maps.google.com" in result["delivery_address"]
        assert cart_with_pizza.context.get("delivery_location") is not None, (
            "preview_bill must PEEK at the pin, never consume it"
        )

        # And placement still attaches the coordinates.
        placed = tools.place_order(
            db, cart_with_pizza, payment_method="cod", contact_name="Bilal",
        )
        order = db.scalar(select(Order).where(Order.order_number == placed["order_number"]))
        assert order.delivery_lat == 24.8607
        assert order.delivery_lng == 67.0011

    def test_direct_tool_call_without_an_outbound_is_exempt(self, db, cart_with_pizza):
        """Same escape hatch as every other guard: a seed/test caller that never drives
        a conversation is unaffected."""
        result = tools.preview_bill(db, cart_with_pizza, payment_method="cod")
        assert "error" not in result, result
        assert result["total"] is not None
