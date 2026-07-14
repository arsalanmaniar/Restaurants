"""JazzCash adapter — page-redirection (HTTP POST) flow.

⚠️ WRITTEN WITHOUT MERCHANT CREDENTIALS.

Everything tagged UNVERIFIED below was inferred from JazzCash's public sandbox
documentation and common community implementations, NOT confirmed against a live
sandbox. Each must be checked with a real test transaction before production. They are
isolated in this one file on purpose: fixing them should not touch anything else.

See docs/PAYMENTS_PLAN.md §3.
"""

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

# UNVERIFIED (1): JazzCash amounts are widely implemented as the LOWEST DENOMINATION
# (paisa) — Rs. 2,780.00 is sent as "278000". Their docs do not state this outright.
# Getting it wrong charges 100x or 1/100x. VERIFY IN SANDBOX FIRST.
AMOUNT_IN_PAISA = True

# UNVERIFIED (2): the response code that means success. "000" is the widely used value.
SUCCESS_CODE = "000"

# UNVERIFIED (3): field name of the gateway's own reference in the callback. Their docs
# mention pp_RetreivalReferenceNo (note their spelling of "Retreival").
PROVIDER_REF_FIELD = "pp_RetreivalReferenceNo"

TXN_EXPIRY = timedelta(hours=1)


class JazzCashProvider(PaymentProvider):
    name = PaymentProviderName.JAZZCASH

    def __init__(self) -> None:
        self.merchant_id = settings.jazzcash_merchant_id
        self.password = settings.jazzcash_password
        self.salt = settings.jazzcash_integrity_salt
        self.post_url = settings.jazzcash_post_url

        if not all([self.merchant_id, self.password, self.salt]):
            raise ProviderNotConfigured(
                "JazzCash credentials are not set (JAZZCASH_MERCHANT_ID / _PASSWORD / "
                "_INTEGRITY_SALT). Apply for a merchant account — see docs/PAYMENTS_PLAN.md."
            )

    # ---------------------------------------------------------------- signing

    def _secure_hash(self, fields: dict[str, str]) -> str:
        """UNVERIFIED (4): the documented pattern is HMAC-SHA256 over the integrity salt
        followed by every non-empty `pp_*` field sorted by key, joined with '&'.

        The salt is BOTH the leading element and the HMAC key in the common
        implementations, which is unusual enough to be worth confirming.
        """
        parts = [self.salt]
        for key in sorted(fields):
            if key.startswith("pp_") and fields[key] != "":
                parts.append(str(fields[key]))

        message = "&".join(parts)
        return (
            hmac.new(self.salt.encode(), message.encode(), hashlib.sha256)
            .hexdigest()
            .upper()
        )

    def _amount_field(self, rupees: Decimal) -> str:
        if AMOUNT_IN_PAISA:
            # Quantize first: Decimal("2780.00") * 100 must be exactly 278000, and float
            # arithmetic here would be an unforgivable way to lose money.
            return str(int(rupees.quantize(Decimal("0.01")) * 100))
        return f"{rupees:.2f}"

    def _amount_from_field(self, raw: str | None) -> Decimal | None:
        if raw in (None, ""):
            return None
        value = Decimal(str(raw))
        return value / 100 if AMOUNT_IN_PAISA else value

    # ---------------------------------------------------------------- interface

    def create_checkout(self, request: CheckoutRequest) -> Checkout:
        now = datetime.now(PAKISTAN_TZ)

        fields: dict[str, str] = {
            "pp_Version": "1.1",
            # MWALLET = JazzCash mobile wallet. MIGS would be card.
            "pp_TxnType": "MWALLET",
            "pp_Language": "EN",
            "pp_MerchantID": self.merchant_id,
            "pp_Password": self.password,
            "pp_TxnRefNo": request.txn_ref,
            "pp_Amount": self._amount_field(request.amount),
            "pp_TxnCurrency": "PKR",
            # UNVERIFIED (5): timestamps are yyyyMMddHHmmss in PKT, not UTC.
            "pp_TxnDateTime": now.strftime("%Y%m%d%H%M%S"),
            "pp_TxnExpiryDateTime": (now + TXN_EXPIRY).strftime("%Y%m%d%H%M%S"),
            "pp_BillReference": request.txn_ref,
            "pp_Description": request.description[:100],
            "pp_ReturnURL": request.return_url,
            "ppmpf_1": request.customer_number or "",
        }
        fields["pp_SecureHash"] = self._secure_hash(fields)

        # Never log pp_Password or the hash.
        safe = {k: v for k, v in fields.items() if k not in ("pp_Password", "pp_SecureHash")}
        return Checkout(post_url=self.post_url, fields=fields, raw_request=safe)

    def verify_callback(self, payload: dict[str, Any]) -> CallbackResult:
        received = str(payload.get("pp_SecureHash", ""))
        expected = self._secure_hash({k: str(v) for k, v in payload.items()})

        # Constant-time: a plain != leaks the hash a character at a time to anyone
        # willing to measure response latency.
        if not received or not hmac.compare_digest(received.upper(), expected):
            raise SignatureError("JazzCash callback failed signature verification")

        code = str(payload.get("pp_ResponseCode", ""))
        successful = code == SUCCESS_CODE

        return CallbackResult(
            txn_ref=str(payload.get("pp_TxnRefNo", "")),
            successful=successful,
            provider_ref=payload.get(PROVIDER_REF_FIELD) or None,
            amount=self._amount_from_field(payload.get("pp_Amount")),
            failure_reason=None
            if successful
            else f"{code}: {payload.get('pp_ResponseMessage', 'unknown error')}",
            raw=dict(payload),
        )

    def query_status(self, txn_ref: str) -> CallbackResult:
        # JazzCash exposes a transaction status inquiry API. Implementing it needs live
        # credentials to test against, so it is deliberately left unimplemented rather
        # than guessed at — a status check that silently returns "not paid" would make
        # the reconciliation job cancel paid orders.
        raise NotImplementedError(
            "JazzCash status inquiry is not implemented yet — needs sandbox credentials. "
            "Until then the reconciliation job will not resolve JazzCash payments and "
            "will leave them for a human (see services/reconciliation.py)."
        )
