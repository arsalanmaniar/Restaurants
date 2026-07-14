import enum


class RestaurantStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class OrderStatus(str, enum.Enum):
    # Prepaid orders start here and are INVISIBLE to the restaurant. Without this a
    # kitchen would start cooking before the customer has paid. COD orders skip it
    # and start at PENDING.
    AWAITING_PAYMENT = "awaiting_payment"
    PENDING = "pending"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentMethod(str, enum.Enum):
    COD = "cod"
    JAZZCASH = "jazzcash"
    EASYPAISA = "easypaisa"
    CARD = "card"


class PaymentStatus(str, enum.Enum):
    """Payment state of the ORDER (the answer to 'has this been paid for?')."""

    UNPAID = "unpaid"
    PAID = "paid"
    REFUNDED = "refunded"


class PaymentProviderName(str, enum.Enum):
    JAZZCASH = "jazzcash"
    EASYPAISA = "easypaisa"
    # Used by tests and local dev so the whole flow can run without merchant
    # credentials. Rejected at runtime unless DEBUG is on — see payments/registry.py.
    FAKE = "fake"


class RefundStatus(str, enum.Enum):
    # Recorded by an admin but not yet pushed to the gateway (or, for COD, not yet
    # handed back in cash). This is the state a refund sits in until a human or the
    # provider API confirms the money actually moved.
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class PaymentAttemptStatus(str, enum.Enum):
    """State of a single attempt at paying. One order can have several — the customer's
    first try fails, they retry. Distinct from PaymentStatus, which is the order's."""

    INITIATED = "initiated"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class ConversationState(str, enum.Enum):
    """Coarse step marker. The AI drives the flow; this is for analytics and
    for resuming a customer who goes quiet mid-order."""

    GREETING = "greeting"
    BROWSING = "browsing"
    ORDERING = "ordering"
    AWAITING_ADDRESS = "awaiting_address"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ORDER_PLACED = "order_placed"
    HUMAN_HANDOFF = "human_handoff"


class MessageDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class StaffRole(str, enum.Enum):
    OWNER = "owner"
    MANAGER = "manager"
    STAFF = "staff"


class CouponDiscountType(str, enum.Enum):
    PERCENTAGE = "percentage"
    FIXED = "fixed"
