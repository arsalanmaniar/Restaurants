"""The payment provider interface.

JazzCash and EasyPaisa are two implementations of one idea: *take money, and tell me
when it lands*. Everything provider-specific — field names, hash algorithms, amount
units — stays behind this interface. If `pp_Amount` ever appears in the order service,
something has gone wrong.

Adding a third provider should mean writing one new file and nothing else.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models import PaymentProviderName


@dataclass
class CheckoutRequest:
    """What the gateway needs in order to show the customer a payment page."""

    txn_ref: str  # our reference; becomes the gateway's transaction id
    amount: Decimal  # in RUPEES. Providers convert to their own units.
    description: str
    return_url: str  # where the gateway sends the customer's browser afterwards
    customer_number: str | None = None


@dataclass
class Checkout:
    """A ready-to-submit payment form.

    We return the target URL and the fields rather than a finished HTML page so the
    provider stays free of presentation concerns, and so tests can assert on the exact
    values being sent.
    """

    post_url: str
    fields: dict[str, str]
    raw_request: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallbackResult:
    """The gateway's verdict on a payment, normalised."""

    txn_ref: str
    successful: bool
    provider_ref: str | None = None
    # In RUPEES, as reported by the gateway. ALWAYS compared against our own recorded
    # amount before we mark anything paid — see payments/service.py.
    amount: Decimal | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class SignatureError(Exception):
    """The callback's hash did not verify. Treat as hostile, not as a failed payment."""


class ProviderNotConfigured(Exception):
    """Credentials are missing. Never fall back to 'just trust the callback'."""


class PaymentProvider(ABC):
    name: PaymentProviderName

    @abstractmethod
    def create_checkout(self, request: CheckoutRequest) -> Checkout:
        """Build the form that sends the customer to the gateway."""

    @abstractmethod
    def verify_callback(self, payload: dict[str, Any]) -> CallbackResult:
        """Validate the gateway's signature and normalise the result.

        MUST raise SignatureError if the hash does not verify. This method is the only
        thing standing between the public internet and 'this order is paid' — an
        unverified callback endpoint lets anyone who learns the URL get free food.
        """

    @abstractmethod
    def query_status(self, txn_ref: str) -> CallbackResult:
        """Ask the gateway what happened to a transaction.

        Used by the reconciliation job, because callbacks get lost and we cannot leave
        a customer's money in limbo waiting for one.
        """
