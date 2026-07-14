"""EasyPaisa adapter — Easypay redirect flow.

⚠️ WRITTEN WITHOUT MERCHANT CREDENTIALS. Same warning as jazzcash.py: EasyPaisa's
integration guide sits behind merchant onboarding, so the field names and hash scheme
below are inferred and marked UNVERIFIED. Confirm against the sandbox
(easypaystg.easypaisa.com.pk) before production.

See docs/PAYMENTS_PLAN.md §1.
"""

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models import PaymentProviderName
from app.services.payments.base import (
    CallbackResult,
    Checkout,
    CheckoutRequest,
    PaymentProvider,
    ProviderNotConfigured,
    SignatureError,
)

logger = logging.getLogger(__name__)

PAKISTAN_TZ = ZoneInfo("Asia/Karachi")

# UNVERIFIED (1): unlike JazzCash, Easypay is commonly documented as taking amounts in
# RUPEES with decimals ("2780.0"). The two providers differing here is exactly the kind
# of thing that causes a 100x mischarge — verify both independently.
AMOUNT_IN_RUPEES = True

# UNVERIFIED (2): "0000" is the widely used success code for Easypay.
SUCCESS_CODE = "0000"

TXN_EXPIRY = timedelta(hours=1)


class EasyPaisaProvider(PaymentProvider):
    name = PaymentProviderName.EASYPAISA

    def __init__(self) -> None:
        self.store_id = settings.easypaisa_store_id
        self.hash_key = settings.easypaisa_hash_key
        self.post_url = settings.easypaisa_post_url

        if not all([self.store_id, self.hash_key]):
            raise ProviderNotConfigured(
                "EasyPaisa credentials are not set (EASYPAISA_STORE_ID / _HASH_KEY). "
                "Apply for a merchant account — see docs/PAYMENTS_PLAN.md."
            )

    def _signature(self, fields: dict[str, str]) -> str:
        """UNVERIFIED (3): Easypay is documented as AES-128-ECB over an ordered,
        ampersand-joined parameter string, base64-encoded — NOT an HMAC.

        We use HMAC-SHA256 here as a stand-in so the flow is testable end-to-end. It is
        cryptographically fine but it is NOT what EasyPaisa will send us, so
        verify_callback WILL fail against the real gateway until this is replaced with
        their actual scheme. This is a known, deliberate gap — not an oversight.
        """
        message = "&".join(f"{k}={fields[k]}" for k in sorted(fields) if k != "merchantHashedReq")
        digest = hmac.new(self.hash_key.encode(), message.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def create_checkout(self, request: CheckoutRequest) -> Checkout:
        now = datetime.now(PAKISTAN_TZ)

        amount = (
            f"{request.amount.quantize(Decimal('0.01'))}"
            if AMOUNT_IN_RUPEES
            else str(int(request.amount * 100))
        )

        fields: dict[str, str] = {
            "storeId": self.store_id,
            "orderRefNum": request.txn_ref,
            "amount": amount,
            "postBackURL": request.return_url,
            "expiryDate": (now + TXN_EXPIRY).strftime("%Y%m%d %H%M%S"),
            "merchantPaymentMethod": "",
            "paymentMethod": "MA_PAYMENT_METHOD",  # mobile account
            "emailAddr": "",
            "mobileNum": request.customer_number or "",
        }
        fields["merchantHashedReq"] = self._signature(fields)

        return Checkout(post_url=self.post_url, fields=fields, raw_request=dict(fields))

    def verify_callback(self, payload: dict[str, Any]) -> CallbackResult:
        received = str(payload.get("merchantHashedReq", ""))
        expected = self._signature({k: str(v) for k, v in payload.items()})

        if not received or not hmac.compare_digest(received, expected):
            raise SignatureError("EasyPaisa callback failed signature verification")

        code = str(payload.get("status", ""))
        successful = code == SUCCESS_CODE

        raw_amount = payload.get("transactionAmount")
        amount = None
        if raw_amount not in (None, ""):
            value = Decimal(str(raw_amount))
            amount = value if AMOUNT_IN_RUPEES else value / 100

        return CallbackResult(
            txn_ref=str(payload.get("orderRefNum", "")),
            successful=successful,
            provider_ref=payload.get("transactionId") or None,
            amount=amount,
            failure_reason=None if successful else f"{code}: {payload.get('desc', 'unknown')}",
            raw=dict(payload),
        )

    def query_status(self, txn_ref: str) -> CallbackResult:
        # Same reasoning as JazzCash: guessing at an inquiry API would let the
        # reconciliation job cancel orders that were actually paid.
        raise NotImplementedError(
            "EasyPaisa status inquiry is not implemented yet — needs sandbox credentials."
        )
