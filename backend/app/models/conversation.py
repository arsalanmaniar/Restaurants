from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ConversationState, MessageDirection


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[ConversationState] = mapped_column(
        Enum(ConversationState, name="conversation_state"),
        default=ConversationState.GREETING,
        nullable=False,
    )
    # The restaurant the customer is currently ordering from, if they've picked one.
    active_restaurant_id: Mapped[int | None] = mapped_column(
        ForeignKey("restaurants.id", ondelete="SET NULL")
    )
    # In-progress cart. Shape:
    #   {"items": [{"menu_item_id": 1, "name": "...", "price": "450.00",
    #               "quantity": 2, "notes": null}]}
    # Lives here rather than Redis so a cart survives a restart and is inspectable
    # from the dashboard during a support call.
    cart: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"items": []}, nullable=False
    )
    # Scratch space for the AI: last shown menu, pending address, etc.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    # Set when the AI gives up and a human needs to take over.
    handoff_reason: Mapped[str | None] = mapped_column(Text)

    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    customer: Mapped["Customer"] = relationship(back_populates="conversations")  # noqa: F821
    messages: Mapped[list["MessageLog"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="MessageLog.id"
    )


class MessageLog(Base):
    __tablename__ = "messages_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, name="message_direction"), nullable=False
    )
    content: Mapped[str | None] = mapped_column(Text)
    # Wassender's own message id — used to drop duplicate webhook deliveries.
    provider_message_id: Mapped[str | None] = mapped_column(String(128), index=True)
    # Raw provider payload / tool-call trace, kept for debugging the AI.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
