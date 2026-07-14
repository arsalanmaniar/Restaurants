"""Coupons.

The one rule that matters more than any other: the PLATFORM funds every discount,
never the restaurant. The restaurant is always paid as if the coupon did not exist;
only the platform's own commission shrinks — and it is never allowed to go negative.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models import Coupon, CouponDiscountType, CouponRedemption, Order
from app.services import conversations as convo
from app.services import coupons as coupons_service
from app.services import tools


def _make_coupon(db, **kwargs) -> Coupon:
    defaults: dict = dict(
        code="TESTCODE",
        discount_type=CouponDiscountType.FIXED,
        value=Decimal("200.00"),
        min_order_amount=Decimal("0.00"),
        is_active=True,
    )
    defaults.update(kwargs)
    coupon = Coupon(**defaults)
    db.add(coupon)
    db.flush()
    return coupon


class TestCommissionMath:
    def test_worked_example_from_the_spec(self, db, conversation, pizza, menu_item):
        """Rs. 2000 subtotal, Rs. 200 coupon, 15% commission -> commission = Rs. 100,
        NOT 15% of the discounted 1800."""
        pizza.commission_rate = Decimal("15.00")
        pizza.min_order_amount = Decimal("0.00")
        menu_item.price = Decimal("2000.00")
        db.flush()

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        _make_coupon(db, code="SAVE200", value=Decimal("200.00"))

        result = tools.place_order(
            db, conversation, delivery_address="House 1", coupon_code="save200"
        )
        assert "error" not in result, result
        db.flush()

        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
        assert order.subtotal == Decimal("2000.00")
        assert order.discount_amount == Decimal("200.00")
        assert order.commission_amount == Decimal("100.00")
        # The restaurant is paid on the FULL subtotal — the coupon is entirely the
        # platform's cost, not deducted from what the restaurant earns.
        assert order.total_amount == order.subtotal + order.delivery_fee - order.discount_amount

    def test_commission_clamps_at_zero_never_negative(self, db, conversation, pizza, menu_item):
        """A coupon bigger than the commission is the platform paying to acquire the
        order — a legitimate choice, but revenue must never go negative for it."""
        pizza.commission_rate = Decimal("15.00")
        pizza.min_order_amount = Decimal("0.00")
        menu_item.price = Decimal("1000.00")
        db.flush()

        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        # Commission on 1000 at 15% is 150 — this coupon takes 500, well past that.
        _make_coupon(db, code="HUGEDEAL", value=Decimal("500.00"))

        result = tools.place_order(
            db, conversation, delivery_address="House 1", coupon_code="HUGEDEAL"
        )
        assert "error" not in result, result
        db.flush()

        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
        assert order.discount_amount == Decimal("500.00")  # the customer still gets it in full
        assert order.commission_amount == Decimal("0.00")  # but revenue never goes negative


class TestDateRange:
    """Evaluated in Asia/Karachi — 'today' is Karachi's calendar day, not UTC's."""

    def test_expired_by_karachi_calendar_even_though_utc_date_is_unchanged(
        self, db, pizza, customer
    ):
        _make_coupon(db, code="OLDDEAL", valid_to=date(2026, 7, 14))

        # 2026-07-14 20:00 UTC is 2026-07-15 01:00 in Karachi (UTC+5) — already the
        # NEXT Karachi day, so this must be expired even though the UTC calendar date
        # is still the 14th.
        at = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
        try:
            coupons_service.validate_coupon(
                db,
                code="OLDDEAL",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("500.00"),
                at=at,
            )
            raise AssertionError("expected an expired coupon to be rejected")
        except coupons_service.CouponError as exc:
            assert "expired" in str(exc)

    def test_still_valid_earlier_the_same_karachi_day(self, db, pizza, customer):
        _make_coupon(db, code="STILLGOOD", valid_to=date(2026, 7, 14))

        # 2026-07-14 10:00 UTC is 2026-07-14 15:00 in Karachi — same Karachi day.
        at = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
        application = coupons_service.validate_coupon(
            db,
            code="STILLGOOD",
            restaurant_id=pizza.id,
            customer_id=customer.id,
            subtotal=Decimal("500.00"),
            at=at,
        )
        assert application.discount_amount > 0

    def test_not_yet_valid_is_rejected(self, db, pizza, customer):
        _make_coupon(db, code="FUTUREDEAL", valid_from=date(2099, 1, 1))

        try:
            coupons_service.validate_coupon(
                db,
                code="FUTUREDEAL",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("500.00"),
            )
            raise AssertionError("expected a not-yet-valid coupon to be rejected")
        except coupons_service.CouponError as exc:
            assert "not valid yet" in str(exc)


class TestUsageLimits:
    def test_usage_cap_exhausted_blocks_even_a_new_customer(self, db, cod_order, pizza, customer):
        coupon = _make_coupon(db, code="LIMITED", usage_limit=1)
        other = convo.get_or_create_customer(db, "923005550001")
        db.flush()
        db.add(
            CouponRedemption(
                coupon_id=coupon.id,
                customer_id=other.id,
                order_id=cod_order.id,
                amount_discounted=Decimal("10.00"),
            )
        )
        db.flush()

        try:
            coupons_service.validate_coupon(
                db,
                code="LIMITED",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("1000.00"),
            )
            raise AssertionError("expected the usage cap to block a new customer")
        except coupons_service.CouponError as exc:
            assert "usage limit" in str(exc)

    def test_same_customer_cannot_reuse_a_coupon(self, db, cod_order, pizza, customer):
        coupon = _make_coupon(db, code="ONCEONLY")
        db.add(
            CouponRedemption(
                coupon_id=coupon.id,
                customer_id=customer.id,
                order_id=cod_order.id,
                amount_discounted=Decimal("50.00"),
            )
        )
        db.flush()

        try:
            coupons_service.validate_coupon(
                db,
                code="ONCEONLY",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("1000.00"),
            )
            raise AssertionError("expected a repeat redemption to be rejected")
        except coupons_service.CouponError as exc:
            assert "already been used" in str(exc)

    def test_place_order_records_the_redemption_and_blocks_a_second_use(
        self, db, conversation, pizza, menu_item
    ):
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=1)
        coupon = _make_coupon(db, code="ONECUST", value=Decimal("50.00"))

        first = tools.place_order(
            db, conversation, delivery_address="House 1", coupon_code="onecust"
        )
        assert "error" not in first, first
        db.flush()

        redemptions = db.scalars(
            select(CouponRedemption).where(CouponRedemption.coupon_id == coupon.id)
        ).all()
        assert len(redemptions) == 1
        assert redemptions[0].customer_id == conversation.customer_id
        assert redemptions[0].amount_discounted == Decimal("50.00")

        # A different-shaped cart (quantity 2, not 1) so the duplicate-order guard in
        # place_order doesn't intercept this before the coupon check even runs.
        tools.get_menu(db, conversation, restaurant_id=pizza.id)
        tools.add_to_cart(db, conversation, menu_item_id=menu_item.id, quantity=2)
        second = tools.place_order(
            db, conversation, delivery_address="House 1", coupon_code="onecust"
        )
        assert second["error"] == "invalid_coupon"
        assert "already been used" in second["message"]


class TestOtherEligibilityRules:
    def test_below_minimum_order_value_is_rejected(self, db, pizza, customer):
        _make_coupon(db, code="BIGORDER", min_order_amount=Decimal("5000.00"))

        try:
            coupons_service.validate_coupon(
                db,
                code="BIGORDER",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("1000.00"),
            )
            raise AssertionError("expected the minimum-order rule to reject this")
        except coupons_service.CouponError as exc:
            assert "minimum order" in str(exc)

    def test_percentage_coupon_respects_max_discount_amount(self, db, pizza, customer):
        _make_coupon(
            db,
            code="PCT50",
            discount_type=CouponDiscountType.PERCENTAGE,
            value=Decimal("50.00"),
            max_discount_amount=Decimal("300.00"),
        )

        application = coupons_service.validate_coupon(
            db,
            code="PCT50",
            restaurant_id=pizza.id,
            customer_id=customer.id,
            subtotal=Decimal("2000.00"),  # 50% would be 1000, capped to 300
        )
        assert application.discount_amount == Decimal("300.00")

    def test_restaurant_specific_coupon_rejected_at_a_different_restaurant(
        self, db, pizza, biryani, customer
    ):
        _make_coupon(db, code="PIZZAONLY", restaurant_id=pizza.id)

        try:
            coupons_service.validate_coupon(
                db,
                code="PIZZAONLY",
                restaurant_id=biryani.id,
                customer_id=customer.id,
                subtotal=Decimal("1000.00"),
            )
            raise AssertionError("expected a restaurant-scoped coupon to be rejected elsewhere")
        except coupons_service.CouponError as exc:
            assert "not valid at this restaurant" in str(exc)

    def test_platform_wide_coupon_works_at_any_restaurant(self, db, pizza, biryani, customer):
        _make_coupon(db, code="ANYWHERE", restaurant_id=None)

        for restaurant in (pizza, biryani):
            application = coupons_service.validate_coupon(
                db,
                code="ANYWHERE",
                restaurant_id=restaurant.id,
                customer_id=customer.id,
                subtotal=Decimal("1000.00"),
            )
            assert application.discount_amount > 0

    def test_unknown_code_is_a_clean_error_not_a_crash(self, db, pizza, customer):
        try:
            coupons_service.validate_coupon(
                db,
                code="DOESNOTEXIST",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("500.00"),
            )
            raise AssertionError("expected an unknown code to be rejected")
        except coupons_service.CouponError as exc:
            assert "not a valid coupon" in str(exc)

    def test_inactive_coupon_is_rejected(self, db, pizza, customer):
        _make_coupon(db, code="DEADCODE", is_active=False)

        try:
            coupons_service.validate_coupon(
                db,
                code="DEADCODE",
                restaurant_id=pizza.id,
                customer_id=customer.id,
                subtotal=Decimal("500.00"),
            )
            raise AssertionError("expected an inactive coupon to be rejected")
        except coupons_service.CouponError as exc:
            assert "no longer active" in str(exc)

    def test_case_insensitive_match(self, db, pizza, customer):
        _make_coupon(db, code="MIXEDCASE")  # stored upper-cased, as the model expects
        application = coupons_service.validate_coupon(
            db,
            code="mixedcase",
            restaurant_id=pizza.id,
            customer_id=customer.id,
            subtotal=Decimal("500.00"),
        )
        assert application.discount_amount > 0

    def test_place_order_with_an_invalid_coupon_is_a_clean_tool_error(self, db, cart_with_pizza):
        """The AI passes a code; the backend must never blow up on a bad one."""
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="House 1", coupon_code="NOSUCHCODE"
        )
        assert result["error"] == "invalid_coupon"
        assert "message" in result


class TestAdminCouponApi:
    def test_admin_can_create_a_coupon(self, client, admin_headers, pizza):
        response = client.post(
            "/admin/coupons",
            headers=admin_headers,
            json={
                "code": "welcome10",
                "discount_type": "percentage",
                "value": "10.00",
                "restaurant_id": pizza.id,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["code"] == "WELCOME10"  # normalized upper-case
        assert body["times_redeemed"] == 0
        assert body["restaurant_name"] == pizza.name

    def test_restaurant_staff_cannot_create_a_coupon(self, client, pizza_headers):
        response = client.post(
            "/admin/coupons",
            headers=pizza_headers,
            json={"code": "STAFFNO", "discount_type": "fixed", "value": "50.00"},
        )
        assert response.status_code == 403

    def test_restaurant_staff_cannot_list_coupons(self, client, pizza_headers):
        assert client.get("/admin/coupons", headers=pizza_headers).status_code == 403

    def test_restaurant_staff_cannot_delete_a_coupon(self, db, client, pizza_headers):
        coupon = _make_coupon(db, code="STAYPUT")
        response = client.delete(f"/admin/coupons/{coupon.id}", headers=pizza_headers)
        assert response.status_code == 403

    def test_duplicate_code_rejected(self, client, admin_headers):
        payload = {"code": "DUPCODE", "discount_type": "fixed", "value": "50.00"}
        first = client.post("/admin/coupons", headers=admin_headers, json=payload)
        assert first.status_code == 201
        second = client.post("/admin/coupons", headers=admin_headers, json=payload)
        assert second.status_code == 409

    def test_percentage_over_100_rejected(self, client, admin_headers):
        response = client.post(
            "/admin/coupons",
            headers=admin_headers,
            json={"code": "TOOMUCH", "discount_type": "percentage", "value": "150.00"},
        )
        assert response.status_code == 422

    def test_unknown_restaurant_id_is_a_clean_400(self, client, admin_headers):
        response = client.post(
            "/admin/coupons",
            headers=admin_headers,
            json={
                "code": "BADREST",
                "discount_type": "fixed",
                "value": "50.00",
                "restaurant_id": 999999,
            },
        )
        assert response.status_code == 400

    def test_cannot_delete_a_coupon_with_redemptions(
        self, db, client, admin_headers, cod_order, customer
    ):
        coupon = _make_coupon(db, code="INUSE")
        db.add(
            CouponRedemption(
                coupon_id=coupon.id,
                customer_id=customer.id,
                order_id=cod_order.id,
                amount_discounted=Decimal("10.00"),
            )
        )
        db.flush()

        response = client.delete(f"/admin/coupons/{coupon.id}", headers=admin_headers)
        assert response.status_code == 409

    def test_can_delete_an_unused_coupon(self, db, client, admin_headers):
        coupon = _make_coupon(db, code="UNUSED")
        response = client.delete(f"/admin/coupons/{coupon.id}", headers=admin_headers)
        assert response.status_code == 204

    def test_update_toggles_active(self, db, client, admin_headers):
        coupon = _make_coupon(db, code="TOGGLEME")
        response = client.patch(
            f"/admin/coupons/{coupon.id}", headers=admin_headers, json={"is_active": False}
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False
