"""sales tax (rate + amount) on orders

Revision ID: f7b3e1a02c48
Revises: e4a1c9d72f10
Create Date: 2026-07-24 12:00:00.000000

Phase F (#4 — bill with tax). Adds tax_rate and tax_amount to `orders`.

Rate depends on payment method (15% cash / 8% online) and is frozen at order
time, exactly like commission_rate: the rate may change later but a placed
order's tax must not be recomputed. tax_amount is INCLUDED in total_amount.

Both default to 0 and are non-nullable, so orders placed before this shipped
read back as "no tax" rather than NULL. Additive — no existing column changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7b3e1a02c48'
down_revision: Union[str, None] = 'e4a1c9d72f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('tax_rate', sa.Numeric(5, 2), nullable=False, server_default='0'),
    )
    op.add_column(
        'orders',
        sa.Column('tax_amount', sa.Numeric(10, 2), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('orders', 'tax_amount')
    op.drop_column('orders', 'tax_rate')
