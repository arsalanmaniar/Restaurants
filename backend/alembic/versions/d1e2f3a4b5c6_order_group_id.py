"""order_group_id on orders for linked multi-restaurant orders

Revision ID: d1e2f3a4b5c6
Revises: c1a4d7f2e831
Create Date: 2026-07-21 15:00:00.000000

Phase 7 (F7 — sequential linked orders). Adds a nullable string column to
`orders` so two independent orders placed in one WhatsApp conversation can
be grouped for dashboard visibility.

Design deliberately keeps this as ONE column, not a separate order_groups
table with a foreign key: the group id is a flat identifier with no
per-group metadata for MVP. If we later need shared status / shared
discount at the group level, we normalise then. Single column here keeps
the migration reversible and the blast radius on `orders` schema minimal.

Partial index — the vast majority of orders will be single-restaurant
(NULL here). Indexing only the non-null rows keeps the write cost
negligible while still making the dashboard/support lookup fast.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c1a4d7f2e831'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('order_group_id', sa.String(length=32), nullable=True),
    )
    op.create_index(
        'ix_orders_order_group_id',
        'orders',
        ['order_group_id'],
        postgresql_where=sa.text('order_group_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_orders_order_group_id', table_name='orders')
    op.drop_column('orders', 'order_group_id')
