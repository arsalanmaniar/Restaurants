"""promotions table

Revision ID: c1a4d7f2e831
Revises: db83fb98ae8f
Create Date: 2026-07-21 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c1a4d7f2e831'
down_revision: Union[str, None] = 'db83fb98ae8f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'promotions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('restaurant_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=120), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        # Reuses the existing `coupon_discount_type` Postgres enum created by
        # migration db83fb98ae8f — no new enum type, just a reference.
        sa.Column(
            'discount_type',
            postgresql.ENUM(
                'PERCENTAGE', 'FIXED',
                name='coupon_discount_type',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('discount_value', sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column(
            'applicable_menu_item_ids',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            'min_order_amount',
            sa.Numeric(precision=10, scale=2),
            server_default=sa.text("0.00"),
            nullable=False,
        ),
        sa.Column('max_discount_amount', sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column('valid_from', sa.Date(), nullable=False),
        sa.Column('valid_to', sa.Date(), nullable=False),
        sa.Column(
            'is_active',
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['restaurant_id'], ['restaurants.id'], ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            'valid_from <= valid_to',
            name='ck_promotions_valid_range',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_promotions_restaurant_id'),
        'promotions',
        ['restaurant_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_promotions_restaurant_id'), table_name='promotions')
    op.drop_table('promotions')
    # The coupon_discount_type enum stays — coupons still use it.
