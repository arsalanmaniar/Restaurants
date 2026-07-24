"""delivery contact (name, phone) and location pin on orders

Revision ID: e4a1c9d72f10
Revises: d1e2f3a4b5c6
Create Date: 2026-07-24 10:00:00.000000

Phase D (#1 — delivery address collection). Adds four nullable snapshot columns
to `orders`:

  * contact_name  — who receives the delivery
  * contact_phone — the number the rider calls; MAY differ from the customer's
    WhatsApp number (a landline, or someone else's mobile)
  * delivery_lat / delivery_lng — the map pin the customer shared on WhatsApp

All nullable and additive — existing orders and the existing order flow are
unaffected. Coordinates are snapshotted on the order (not only on
customer_addresses) for the same reason delivery_address_text is: the order
must remember exactly where it was sent, independent of any later profile edit.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a1c9d72f10'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('contact_name', sa.String(length=120), nullable=True))
    op.add_column('orders', sa.Column('contact_phone', sa.String(length=32), nullable=True))
    op.add_column('orders', sa.Column('delivery_lat', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('delivery_lng', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'delivery_lng')
    op.drop_column('orders', 'delivery_lat')
    op.drop_column('orders', 'contact_phone')
    op.drop_column('orders', 'contact_name')
