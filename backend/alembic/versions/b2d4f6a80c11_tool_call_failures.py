"""tool_call_failures — measure malformed tool calls

Revision ID: b2d4f6a80c11
Revises: ff59bcc37aab
Create Date: 2026-07-31

Additive: a new table only. Nothing existing is read or rewritten, so this is
safe to run against the live database while it is serving traffic.

conversation_id is deliberately NOT a foreign key — see the model for why. An FK
makes the insert BLOCK on any still-open transaction that created the parent
conversation, which is precisely the transaction this recorder runs inside.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2d4f6a80c11'
down_revision: Union[str, None] = 'ff59bcc37aab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_call_failures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("gave_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tool_name", sa.String(length=64), nullable=True),
        sa.Column("failed_generation", sa.Text(), nullable=True),
        sa.Column("generation_length", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_keys", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_tool_call_failures_turn_id", "tool_call_failures", ["turn_id"])
    op.create_index(
        "ix_tool_call_failures_conversation_id", "tool_call_failures", ["conversation_id"]
    )
    op.create_index("ix_tool_call_failures_tool_name", "tool_call_failures", ["tool_name"])
    op.create_index("ix_tool_call_failures_created_at", "tool_call_failures", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_call_failures_created_at", table_name="tool_call_failures")
    op.drop_index("ix_tool_call_failures_tool_name", table_name="tool_call_failures")
    op.drop_index("ix_tool_call_failures_conversation_id", table_name="tool_call_failures")
    op.drop_index("ix_tool_call_failures_turn_id", table_name="tool_call_failures")
    op.drop_table("tool_call_failures")
