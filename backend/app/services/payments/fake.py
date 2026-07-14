"""A fake gateway, so the entire payment flow can be built and tested with no merchant
credentials and no network.

This is not a toy. It implements the same interface with real HMAC signing, so the
callback verification, amount tampering checks, idempotency, and reconciliation logic
are all exercised for real. When JazzCash credentials arrive, only the adapter changes.

Refuses to load unless DEBUG is on — see registry.py. A fake payment provider reachable
in production would mean anyone could mark an order paid.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.models import PaymentProviderName
from app.services.payments.base import (
    CallbackResult,
    Checkout,
    CheckoutRequest,
    PaymentProvider,
    SignatureError,
)

TXN_EXPIRY = timedelta(hours=1)

# What query_status() should report, so tests can drive the reconciliation job.
# Maps txn_ref -> (successful, amount).
STATUS_ORACLE: dict[str, tuple[bool, Decimal | None]] = {}


class FakeProvider(PaymentProvider):
    name = PaymentProviderName.FAKE

    def __init__(self) -> None:
        self.secret = settings.jwt_secret  # any stable secret will do for a fake

    def _sign(self, fields: dict[str, Any]) -> str:
        message = "&".join(
            f"{k}={fields[k]}" for k in sorted(fields) if k != "signature" and fields[k] != ""
        )
        return hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    def create_checkout(self, request: CheckoutRequest) -> Checkout:
        fields = {
            "txn_ref": request.txn_ref,
            "amount": f"{request.amount:.2f}",
            "return_url": request.return_url,
            "expires": (datetime.now(timezone.utc) + TXN_EXPIRY).isoformat(),
        }
        fields["signature"] = self._sign(fields)
        return Checkout(
            post_url="https://fake-gateway.invalid/checkout",
            fields=fields,
            raw_request=dict(fields),
        )

    def verify_callback(self, payload: dict[str, Any]) -> CallbackResult:
        received = str(payload.get("signature", ""))
        expected = self._sign({k: str(v) for k, v in payload.items()})

        if not received or not hmac.compare_digest(received, expected):
            raise SignatureError("fake callback failed signature verification")

        successful = str(payload.get("status", "")) == "paid"
        raw_amount = payload.get("amount")

        return CallbackResult(
            txn_ref=str(payload.get("txn_ref", "")),
            successful=successful,
            provider_ref=payload.get("provider_ref") or f"FAKE-{payload.get('txn_ref')}",
            amount=Decimal(str(raw_amount)) if raw_amount not in (None, "") else None,
            failure_reason=None if successful else str(payload.get("reason", "declined")),
            raw=dict(payload),
        )

    def query_status(self, txn_ref: str) -> CallbackResult:
        if txn_ref not in STATUS_ORACLE:
            # Gateway has never heard of it -> treat as failed, same as a real one would.
            return CallbackResult(txn_ref=txn_ref, successful=False,
                                  failure_reason="unknown transaction")

        successful, amount = STATUS_ORACLE[txn_ref]
        return CallbackResult(
            txn_ref=txn_ref,
            successful=successful,
            provider_ref=f"FAKE-{txn_ref}",
            amount=amount,
            failure_reason=None if successful else "declined",
        )

    def sign_callback(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Test helper: produce a correctly-signed callback the way the gateway would."""
        signed = {k: str(v) for k, v in payload.items()}
        signed["signature"] = self._sign(signed)
        return signed
