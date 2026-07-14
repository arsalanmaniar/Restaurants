"""Payment reconciliation.

Callbacks get lost — the gateway retries into a deploy, our host blips, a customer's
network dies mid-redirect. If we only ever learn about payments from callbacks, a
customer who genuinely paid can be left with a cancelled order, which is the worst
outcome available to us.

So this job independently asks the gateway what happened to every payment still sitting
in INITIATED past its expiry, and resolves it:

    gateway says PAID    -> settle it, release the order to the kitchen (late, but right)
    gateway says FAILED  -> expire the attempt, cancel the order
    gateway can't say    -> LEAVE IT ALONE and flag it for a human

That last case is the important one. When we cannot get a straight answer, doing nothing
is correct: cancelling an order that was actually paid is worse than a delayed one.

Run it:  python -m app.services.reconciliation
Schedule it every few minutes (cron / Railway scheduled job / APScheduler).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models import Payment, PaymentAttemptStatus
from app.services.payments.registry import get_provider
from app.services.payments.service import apply_callback, expire_payment
from app.services.payments.base import ProviderNotConfigured

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    checked: int = 0
    settled_late: int = 0
    expired: int = 0
    needs_human: int = 0

    def __str__(self) -> str:
        return (
            f"checked={self.checked} settled_late={self.settled_late} "
            f"expired={self.expired} needs_human={self.needs_human}"
        )


def stale_payments(db: Session, now: datetime | None = None) -> list[Payment]:
    """Attempts still 'initiated' whose window has closed."""
    moment = now or datetime.now(timezone.utc)
    return list(
        db.scalars(
            select(Payment)
            .where(
                Payment.status == PaymentAttemptStatus.INITIATED,
                Payment.expires_at <= moment,
            )
            .order_by(Payment.id)
        ).all()
    )


def reconcile(db: Session, now: datetime | None = None) -> ReconcileReport:
    report = ReconcileReport()

    for payment in stale_payments(db, now):
        report.checked += 1

        try:
            provider = get_provider(payment.provider)
            result = provider.query_status(payment.txn_ref)

        except NotImplementedError:
            # The adapter has no status-inquiry API yet (JazzCash/EasyPaisa both need
            # sandbox credentials to build one). We must NOT assume "not paid" — that
            # would cancel orders the customer has actually paid for. Leave it.
            report.needs_human += 1
            logger.warning(
                "payment %s (%s) is stale but its provider has no status inquiry — "
                "leaving it for a human rather than risk cancelling a paid order",
                payment.txn_ref,
                payment.provider.value,
            )
            continue

        except ProviderNotConfigured:
            report.needs_human += 1
            logger.error("payment %s: provider not configured", payment.txn_ref)
            continue

        except Exception:
            # A network blip must not cancel anyone's order. Try again next run.
            report.needs_human += 1
            logger.exception("payment %s: status inquiry failed", payment.txn_ref)
            continue

        if result.successful:
            # It WAS paid — the callback just never reached us. Settle it properly,
            # reusing the same guarded path (amount check and all) as a live callback.
            try:
                apply_callback(db, result)
                db.commit()
                report.settled_late += 1
                logger.info(
                    "payment %s was paid after all; order released late", payment.txn_ref
                )
            except Exception:
                db.rollback()
                report.needs_human += 1
                logger.exception("payment %s: could not settle late payment", payment.txn_ref)
        else:
            expire_payment(db, payment)
            db.commit()
            report.expired += 1

    return report


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )
    db = SessionLocal()
    try:
        report = reconcile(db)
        logger.info("reconciliation complete: %s", report)
        if report.needs_human:
            logger.warning(
                "%s payment(s) could not be resolved automatically and need review",
                report.needs_human,
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
