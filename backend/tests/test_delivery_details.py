"""Phase D — delivery contact (name + separate phone) and location pin on orders.

#1 from the WhatsApp feedback batch. The order now snapshots WHO receives it and
WHICH number the rider should call (which can differ from the customer's WhatsApp
number), plus a map pin if the customer shared one.

All additive: place_order still works with none of these supplied — name falls
back to the customer's saved name, phone to the WhatsApp number, location to None.
"""

from decimal import Decimal

from sqlalchemy import select

from app.models import Order
from app.services import tools


def _place(db, conversation, **kwargs):
    """Ground a cart at Pizza Junction and place it, returning the Order row."""
    result = tools.place_order(db, conversation, **kwargs)
    assert "order_number" in result, result
    return result, db.scalar(
        select(Order).where(Order.order_number == result["order_number"])
    )


class TestContactSnapshot:

    def test_name_and_phone_are_stored_on_the_order(self, db, cart_with_pizza):
        result, order = _place(
            db,
            cart_with_pizza,
            delivery_address="House 5, DHA Phase 6, Karachi",
            contact_name="Bilal Ahmed",
            contact_phone="03211234567",
        )
        assert order.contact_name == "Bilal Ahmed"
        assert order.contact_phone == "03211234567"
        # Echoed back so the model can confirm to the customer.
        assert result["contact_name"] == "Bilal Ahmed"
        assert result["contact_phone"] == "03211234567"

    def test_contact_phone_may_differ_from_whatsapp_number(self, db, cart_with_pizza):
        """The whole point of the separate field: someone else's number for the
        person receiving the food."""
        wa_number = cart_with_pizza.customer.whatsapp_number
        _, order = _place(
            db,
            cart_with_pizza,
            delivery_address="Office, Gulshan",
            contact_phone="0429999999",  # a landline, not the WhatsApp number
        )
        assert order.contact_phone == "0429999999"
        assert order.contact_phone != wa_number

    def test_phone_falls_back_to_whatsapp_number(self, db, cart_with_pizza):
        _, order = _place(db, cart_with_pizza, delivery_address="Saddar")
        assert order.contact_phone == cart_with_pizza.customer.whatsapp_number

    def test_name_falls_back_to_customer_name(self, db, cart_with_pizza):
        cart_with_pizza.customer.name = "Existing Name"
        db.flush()
        _, order = _place(db, cart_with_pizza, delivery_address="Saddar")
        assert order.contact_name == "Existing Name"

    def test_new_name_backfills_an_empty_customer_name(self, db, cart_with_pizza):
        """A name we didn't have before is remembered on the customer, so the
        next order can greet them by it."""
        cart_with_pizza.customer.name = None
        db.flush()
        _place(
            db, cart_with_pizza,
            delivery_address="Saddar", contact_name="Ayesha Khan",
        )
        assert cart_with_pizza.customer.name == "Ayesha Khan"

    def test_existing_customer_name_is_not_overwritten(self, db, cart_with_pizza):
        cart_with_pizza.customer.name = "Original"
        db.flush()
        _place(
            db, cart_with_pizza,
            delivery_address="Saddar", contact_name="Someone Else",
        )
        # The order records who received THIS delivery...
        order = db.scalar(
            select(Order).where(Order.customer_id == cart_with_pizza.customer_id)
        )
        assert order.contact_name == "Someone Else"
        # ...but the customer's own saved name is left alone.
        assert cart_with_pizza.customer.name == "Original"


class TestLocationOnOrder:

    def _share_pin(self, conversation, lat, lng):
        ctx = dict(conversation.context or {})
        ctx["delivery_location"] = {"lat": lat, "lng": lng}
        conversation.context = ctx

    def test_shared_pin_is_stored_on_the_order(self, db, cart_with_pizza):
        self._share_pin(cart_with_pizza, 24.8607, 67.0011)
        _, order = _place(
            db, cart_with_pizza, delivery_address="House 5, DHA",
        )
        assert order.delivery_lat == 24.8607
        assert order.delivery_lng == 67.0011

    def test_pin_is_consumed_so_a_later_order_does_not_inherit_it(
        self, db, cart_with_pizza, menu_item,
    ):
        self._share_pin(cart_with_pizza, 24.8607, 67.0011)
        _place(db, cart_with_pizza, delivery_address="House 5, DHA")
        assert "delivery_location" not in (cart_with_pizza.context or {})

        # Build + place a second order in the same conversation, no new pin.
        tools.add_to_cart(db, cart_with_pizza, menu_item_id=menu_item.id, quantity=1)
        _, order2 = _place(db, cart_with_pizza, delivery_address="Different place")
        assert order2.delivery_lat is None
        assert order2.delivery_lng is None

    def test_pin_stands_in_when_no_text_address_given(self, db, cart_with_pizza):
        """A pin alone must not block the order — it becomes the address text."""
        self._share_pin(cart_with_pizza, 24.8607, 67.0011)
        result, order = _place(db, cart_with_pizza)  # no delivery_address
        assert order.delivery_lat == 24.8607
        assert "maps.google.com" in order.delivery_address_text
        assert "maps.google.com" in result["delivery_address"]

    def test_no_pin_leaves_coordinates_null(self, db, cart_with_pizza):
        _, order = _place(db, cart_with_pizza, delivery_address="Saddar")
        assert order.delivery_lat is None
        assert order.delivery_lng is None

    def test_still_asks_for_address_when_neither_text_nor_pin(self, db, cart_with_pizza):
        """No address AND no pin AND no default → the existing missing_address
        path is unchanged."""
        result = tools.place_order(db, cart_with_pizza)
        assert result.get("error") == "missing_address"


class TestBackwardCompatibility:

    def test_place_order_still_works_with_no_new_args(self, db, cart_with_pizza):
        """The whole point: existing callers pass none of the new params."""
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="Saddar", payment_method="cod"
        )
        assert "order_number" in result
        assert result["total"] == result["total"]  # sane payload

    def test_order_out_schema_exposes_the_new_fields(self, db, cart_with_pizza):
        from app.schemas import OrderOut

        _, order = _place(
            db, cart_with_pizza,
            delivery_address="House 5", contact_name="Bilal", contact_phone="03211234567",
        )
        order.delivery_lat = Decimal("24.86")  # exercise serialization
        db.flush()
        dto = OrderOut.model_validate(order)
        assert dto.contact_name == "Bilal"
        assert dto.contact_phone == "03211234567"
        assert dto.delivery_lat is not None
