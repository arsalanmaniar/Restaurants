"""Phase D — delivery contact (name + separate phone) and location pin on orders.

#1 from the WhatsApp feedback batch. The order now snapshots WHO receives it and
WHICH number the rider should call (which can differ from the customer's WhatsApp
number), plus a map pin if the customer shared one.

All additive: place_order still works with none of these supplied — name falls
back to the customer's saved name, phone to the WhatsApp number, location to None.
"""

from decimal import Decimal

from sqlalchemy import select

from app.models import MessageDirection, Order
from app.services import conversations as convo_svc
from app.services import tools


def _place(db, conversation, **kwargs):
    """Ground a cart at Pizza Junction and place it, returning the Order row."""
    result = tools.place_order(db, conversation, **kwargs)
    assert "order_number" in result, result
    return result, db.scalar(
        select(Order).where(Order.order_number == result["order_number"])
    )


def _seed_payment_ack(db, conversation):
    """Log an outbound that looks like a real, model-driven flow (and satisfies the
    silent-COD guard, so a COD placement reaches the contact-name guard)."""
    convo_svc.log_message(
        db, conversation, MessageDirection.OUTBOUND,
        "Payment kis se karna hai — cod, jazzcash, ya easypaisa?",
    )
    db.flush()


class TestContactNameRequiredInRealFlow:
    """Issue 3 (AB-F6DF70): in a live conversation, the delivery-contact NAME is a
    hard requirement — the model can no longer silently place with just a bare area
    name. The phone stays optional (falls back to the WhatsApp number)."""

    def _ready(self, db, conversation):
        _seed_payment_ack(db, conversation)
        # Pass the Bug 1 preview guard. The Issue 3 one-bill gate needs the delivery
        # details here too — this helper's callers are testing what place_order does
        # with the NAME, so the preview is given both up front.
        tools.preview_bill(
            db, conversation, payment_method="cod",
            delivery_address="Ramchorline Karachi", contact_name="Ayesha",
        )

    def test_real_flow_without_a_name_is_refused_and_places_nothing(
        self, db, cart_with_pizza,
    ):
        self._ready(db, cart_with_pizza)
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="Ramchorline Karachi", payment_method="cod"
        )
        assert result["error"] == "missing_contact_name"
        assert db.scalar(
            select(Order).where(Order.customer_id == cart_with_pizza.customer_id)
        ) is None

    def test_real_flow_places_once_a_name_is_given(self, db, cart_with_pizza):
        self._ready(db, cart_with_pizza)
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="Ramchorline Karachi",
            payment_method="cod", contact_name="Ayesha",
        )
        assert "order_number" in result, result

    def test_phone_still_defaults_to_the_whatsapp_number(self, db, cart_with_pizza):
        """Name is required; phone is NOT — an unspecified phone falls back to the
        WhatsApp number, so there's no redundant 'phone?' ask in the common case."""
        self._ready(db, cart_with_pizza)
        wa = cart_with_pizza.customer.whatsapp_number
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="Ramchorline Karachi",
            payment_method="cod", contact_name="Ayesha",
        )
        order = db.scalar(select(Order).where(Order.order_number == result["order_number"]))
        assert order.contact_name == "Ayesha"
        assert order.contact_phone == wa

    def test_direct_tool_call_without_an_outbound_does_not_require_a_name(
        self, db, cart_with_pizza,
    ):
        """Escape hatch: a direct-tool / seed caller (no outbound) is unaffected."""
        result = tools.place_order(
            db, cart_with_pizza, delivery_address="Ramchorline Karachi", payment_method="cod"
        )
        assert "order_number" in result, result


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
