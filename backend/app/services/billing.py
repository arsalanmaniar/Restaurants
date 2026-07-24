"""Order bill arithmetic — the single source of truth for money on an order.

Both `preview_bill` (read-only, for the read-back) and `place_order` (which
persists) compute the customer-facing numbers HERE, so the total the customer
confirms and the total we store and charge can never drift apart.

Tax model (confirmed with the business, 2026-07-24):
  * Rate depends on payment method: 15% cash-on-delivery, 8% online. Cash is
    taxed higher to nudge customers toward prepaid.
  * Tax is on the FOOD only (subtotal), never on the delivery fee, and on the
    food NET of any coupon discount — the customer is taxed on what they
    actually pay for food.

Commission is computed separately in place_order (it is internal, never shown to
the customer) but its base — subtotal + tax — is documented here because it is
part of the same money model.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.models import PaymentMethod

# Tax rate by payment method, in percent.
TAX_RATE_COD = Decimal("15")
TAX_RATE_ONLINE = Decimal("8")


def tax_rate_for(method: PaymentMethod) -> Decimal:
    """15% for cash-on-delivery, 8% for any online method."""
    return TAX_RATE_COD if method == PaymentMethod.COD else TAX_RATE_ONLINE


def _money(value: Decimal) -> Decimal:
    """Quantize to 2dp, matching the existing commission rounding in tools.py
    (banker's rounding — Decimal's default). Kept identical so tax and
    commission round the same way."""
    return value.quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Bill:
    """The customer-facing breakdown. All Decimals, all 2dp."""

    subtotal: Decimal
    discount: Decimal
    tax_rate: Decimal
    tax_amount: Decimal
    delivery_fee: Decimal
    total: Decimal


def compute_bill(
    *,
    subtotal: Decimal,
    delivery_fee: Decimal,
    discount: Decimal,
    method: PaymentMethod,
) -> Bill:
    """The bill for a cart, given the payment method (which sets the tax rate).

    total = subtotal + tax + delivery − discount
    tax   = (subtotal − discount) × rate,  never negative

    Pure: no DB, no I/O. The caller supplies subtotal (from the cart), the
    restaurant's delivery_fee, and any coupon discount.
    """
    tax_rate = tax_rate_for(method)
    # Tax on the discounted food. A discount larger than the food subtotal
    # (possible with a generous platform coupon) must not produce negative tax.
    taxable_food = max(subtotal - discount, Decimal("0.00"))
    tax_amount = _money(taxable_food * tax_rate / Decimal("100"))
    total = subtotal + tax_amount + delivery_fee - discount
    return Bill(
        subtotal=subtotal,
        discount=discount,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        delivery_fee=delivery_fee,
        total=total,
    )
