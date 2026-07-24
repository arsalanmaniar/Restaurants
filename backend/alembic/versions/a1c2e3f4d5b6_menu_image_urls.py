"""menu image URLs on restaurants and categories

Revision ID: a1c2e3f4d5b6
Revises: f7b3e1a02c48
Create Date: 2026-07-24 14:00:00.000000

Phase G (#9 — send menu image). Adds a nullable menu_image_url to `restaurants`
(a whole-menu photo) and to `menu_categories` (a per-category photo). URL only —
WhatsApp needs a publicly reachable imageUrl and this backend hosts no files, so
we store a link and pass it straight to Wassender's send-image endpoint.

Additive, both nullable; no existing rows or behaviour change.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c2e3f4d5b6'
down_revision: Union[str, None] = 'f7b3e1a02c48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('restaurants', sa.Column('menu_image_url', sa.String(length=512), nullable=True))
    op.add_column('menu_categories', sa.Column('menu_image_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column('menu_categories', 'menu_image_url')
    op.drop_column('restaurants', 'menu_image_url')
