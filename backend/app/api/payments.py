"""Payment link page and gateway callback webhook."""

import logging
from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.deps import DbSession
from app.models import Payment, PaymentAttemptStatus, PaymentProviderName
from app.services.payments.base import ProviderNotConfigured, SignatureError
from app.services.payments.fake import FakeProvider
from app.services.payments.registry import get_provider
from app.services.payments.service import PaymentError, apply_callback, build_checkout
from app.services.payments.tokens import read_pay_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["payments"])


def _page(title: str, message: str, ok: bool = True) -> HTMLResponse:
    colour = "#059669" if ok else "#dc2626"
    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:90vh;
             align-items:center;justify-content:center;margin:0;background:#f8fafc">
  <div style="text-align:center;padding:2rem;max-width:24rem">
    <h1 style="color:{colour};font-size:1.25rem;margin:0 0 .5rem">{escape(title)}</h1>
    <p style="color:#475569;margin:0">{escape(message)}</p>
    <p style="color:#94a3b8;font-size:.875rem;margin-top:1.5rem">
      You can close this page and return to WhatsApp.</p>
  </div>
</body></html>"""
    )


@router.get("/pay/{token}", response_class=HTMLResponse)
def pay(token: str, db: DbSession) -> HTMLResponse:
    """The page the WhatsApp payment link points at.

    It holds no state of its own: it resolves the signed token to a payment attempt,
    builds the gateway's form, and auto-submits it. The customer's browser then leaves
    for the gateway's own domain, which is where every payment credential is entered —
    never here, and never in the chat.
    """
    payment_id = read_pay_token(token)
    if payment_id is None:
        return _page("Link expired", "This payment link is no longer valid. "
                                     "Message us on WhatsApp for a new one.", ok=False)

    payment = db.get(Payment, payment_id)
    if payment is None:
        return _page("Not found", "We couldn't find that payment.", ok=False)

    # The same guards `build_checkout` runs, hoisted here so they apply to
    # both real gateways AND the FAKE demo path below. Without this an
    # expired payment for a FAKE order would skip straight into the demo
    # checkout page.
    if payment.status != PaymentAttemptStatus.INITIATED:
        return _page("Link used", "This payment link has already been used.", ok=False)
    if payment.expires_at <= datetime.now(timezone.utc):
        return _page(
            "Link expired",
            "This payment link is no longer valid. Message us on WhatsApp for a new one.",
            ok=False,
        )

    # FAKE provider = demo mode. Its post_url is intentionally unreachable
    # (fake-gateway.invalid), so the customer would hit a browser error if we
    # auto-submitted the form to it. Render a demo checkout page with two
    # buttons instead — the customer picks success or failure, which POSTs
    # back to /pay/{token}/demo where we synthesise the signed callback and
    # run it through the SAME apply_callback path a real gateway would.
    if payment.provider == PaymentProviderName.FAKE:
        return _fake_demo_checkout(payment, token)

    try:
        checkout = build_checkout(db, payment)
        db.commit()
    except PaymentError as exc:
        return _page("Cannot pay", str(exc), ok=False)
    except ProviderNotConfigured:
        logger.exception("payment provider not configured")
        return _page("Unavailable", "Online payment is temporarily unavailable.", ok=False)

    inputs = "\n".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(str(v))}">'
        for k, v in checkout.fields.items()
    )

    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Redirecting to payment…</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding-top:4rem;
             background:#f8fafc;color:#475569">
  <p>Taking you to the payment page…</p>
  <form id="f" method="post" action="{escape(checkout.post_url)}">
    {inputs}
    <noscript><button type="submit">Continue to payment</button></noscript>
  </form>
  <script>document.getElementById('f').submit();</script>
</body></html>"""
    )


def _fake_demo_checkout(payment: Payment, token: str) -> HTMLResponse:
    """Two-button demo page shown for FAKE-provider payments (DEBUG mode only —
    real providers auto-submit to their gateway and never reach this branch).

    Clearly labelled "(demo)" so nobody mistakes it for a real payment step."""
    order_no = escape(payment.order.order_number)
    amount = f"{payment.amount:.2f}"
    action = escape(f"/pay/{token}/demo")
    return HTMLResponse(
        f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Demo payment</title></head>
<body style="font-family:system-ui,sans-serif;display:flex;min-height:90vh;
             align-items:center;justify-content:center;margin:0;background:#f8fafc">
  <div style="text-align:center;padding:2rem;max-width:26rem">
    <p style="color:#94a3b8;font-size:.75rem;letter-spacing:.1em;
              text-transform:uppercase;margin:0 0 .75rem">Demo checkout</p>
    <h1 style="color:#0f172a;font-size:1.25rem;margin:0 0 .25rem">
      Order {order_no}
    </h1>
    <p style="color:#475569;margin:0 0 1.5rem;font-size:1.5rem;
              font-weight:600;font-variant-numeric:tabular-nums">
      Rs. {escape(amount)}
    </p>
    <div style="display:flex;flex-direction:column;gap:.5rem">
      <form method="post" action="{action}?outcome=success">
        <button type="submit"
                style="width:100%;padding:.85rem 1rem;border-radius:.5rem;
                       border:0;background:#059669;color:#fff;font-size:.95rem;
                       font-weight:600;cursor:pointer">
          ✅ Simulate successful payment
        </button>
      </form>
      <form method="post" action="{action}?outcome=failure">
        <button type="submit"
                style="width:100%;padding:.85rem 1rem;border-radius:.5rem;
                       border:1px solid #e2e8f0;background:#fff;color:#dc2626;
                       font-size:.95rem;font-weight:600;cursor:pointer">
          ❌ Simulate failed payment
        </button>
      </form>
    </div>
    <p style="color:#94a3b8;font-size:.75rem;margin-top:1.5rem">
      DEBUG mode only — no real money moves.
    </p>
  </div>
</body></html>"""
    )


@router.post("/pay/{token}/demo", response_class=HTMLResponse)
def pay_demo(
    token: str,
    db: DbSession,
    outcome: str = Query("success", pattern="^(success|failure)$"),
) -> HTMLResponse:
    """FakeProvider demo checkout handler.

    Reuses the real callback pipeline: we synthesise a payload that the
    FakeProvider would produce, sign it with FakeProvider.sign_callback(),
    and hand it to apply_callback() — the same function a real gateway's
    signed callback would land in. So every amount-check, idempotency guard,
    and order-release rule runs unchanged.
    """
    payment_id = read_pay_token(token)
    if payment_id is None:
        return _page("Link expired", "This payment link is no longer valid.", ok=False)

    payment = db.get(Payment, payment_id)
    if payment is None:
        return _page("Not found", "We couldn't find that payment.", ok=False)

    if payment.provider != PaymentProviderName.FAKE:
        # A real-provider payment must never be settled through the demo
        # button — the two-button page is not shown for them, and hitting
        # this URL directly would be a bypass.
        return _page("Not allowed", "This endpoint is for demo payments only.", ok=False)

    fake = FakeProvider()
    payload = {
        "txn_ref": payment.txn_ref,
        "amount": f"{payment.amount:.2f}",
        "status": "paid" if outcome == "success" else "declined",
        "reason": "" if outcome == "success" else "customer chose to simulate failure",
        "provider_ref": f"FAKE-{payment.txn_ref}",
    }
    signed = fake.sign_callback(payload)

    try:
        result = fake.verify_callback(signed)
        applied = apply_callback(db, result)
        db.commit()
    except SignatureError:
        logger.exception("demo checkout produced an unsigned payload — bug in _sign()")
        return _page("Error", "Demo signature failed.", ok=False)
    except PaymentError as exc:
        db.rollback()
        return _page("Cannot settle", str(exc), ok=False)

    if applied.status == PaymentAttemptStatus.PAID:
        return _page(
            "Payment successful (demo)",
            f"Order {payment.order.order_number} is confirmed.",
            ok=True,
        )
    return _page(
        "Payment failed (demo)",
        "The order was not confirmed. Message us on WhatsApp for a new link.",
        ok=False,
    )


@router.post("/webhooks/payments/{provider}/callback")
async def payment_callback(provider: str, request: Request, db: DbSession):
    """Where the gateway tells us what happened.

    This endpoint is the entire security boundary around "this order is paid". It must:
      * verify the signature before believing a single field  (provider.verify_callback)
      * never trust the amount                                (service.apply_callback)
      * tolerate replays                                      (service.apply_callback)
    """
    try:
        name = PaymentProviderName(provider)
    except ValueError:
        return JSONResponse({"status": "unknown provider"}, status.HTTP_404_NOT_FOUND)

    # Gateways post form-encoded; accept JSON too so tests and future providers work.
    form = await request.form()
    payload = dict(form) if form else {}
    if not payload:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    if not payload:
        return JSONResponse({"status": "ignored", "reason": "empty payload"})

    try:
        gateway = get_provider(name)
        result = gateway.verify_callback(payload)
    except SignatureError:
        # Not a failed payment — an unauthenticated message claiming to be one.
        logger.error("REJECTED unsigned/forged %s callback: %s", provider, payload)
        return JSONResponse({"status": "rejected"}, status.HTTP_403_FORBIDDEN)
    except ProviderNotConfigured:
        logger.exception("callback for unconfigured provider %s", provider)
        return JSONResponse({"status": "unavailable"}, status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        payment = apply_callback(db, result)
        db.commit()
    except PaymentError as exc:
        # Only raised when we cannot act at all (e.g. a transaction reference we have
        # never issued). Nothing was written, so there is nothing to preserve.
        db.rollback()
        logger.error("callback rejected for %s: %s", result.txn_ref, exc)
        # 200: the gateway's message was authentic and we have recorded our decision.
        # A 4xx would make it retry a callback that will never be accepted.
        return JSONResponse({"status": "rejected", "reason": str(exc)})

    # A recorded failure (declined card, amount mismatch) is committed, not rolled back —
    # the failed attempt and its reason are the audit trail.
    if payment.status == PaymentAttemptStatus.FAILED:
        return JSONResponse(
            {"status": "rejected", "reason": payment.failure_reason or "payment failed"}
        )

    # TODO(V1): notify the customer and the restaurant on WhatsApp here. Gated on the
    # 24-hour-window/template question — see services/whatsapp.py.
    return JSONResponse({"status": "ok", "payment_status": payment.status.value})
