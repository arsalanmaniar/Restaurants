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

from app.models import Order, PaymentAttemptStatus, PaymentMethod
from app.services import billing
from app.services import tools


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
