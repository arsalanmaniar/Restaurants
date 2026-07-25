"""add online payment method

Revision ID: ff59bcc37aab
Revises: a1c2e3f4d5b6
Create Date: 2026-07-25 22:28:25.099648

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'ff59bcc37aab'
down_revision: Union[str, None] = 'a1c2e3f4d5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the generic ONLINE value to the payment_method enum. SQLAlchemy's Enum
    # persists the member NAME (the existing labels are COD/JAZZCASH/EASYPAISA/CARD,
    # not the lowercase .value), so the label MUST be 'ONLINE' to match how the ORM
    # stores PaymentMethod.ONLINE. ADD VALUE runs inside Alembic's transaction on
    # PostgreSQL 12+ as long as the new value is not USED in the same transaction
    # (it isn't here). Idempotent, so a re-run is safe.
    op.execute("ALTER TYPE payment_method ADD VALUE IF NOT EXISTS 'ONLINE'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type, and leaving 'online' in
    # place is harmless. This downgrade is intentionally a no-op.
    pass
