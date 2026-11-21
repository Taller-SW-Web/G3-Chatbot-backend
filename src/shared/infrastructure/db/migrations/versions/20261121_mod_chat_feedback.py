"""Customer feedback tables and order_ref.csat_sent (SPEC-24).

Revision ID: 20261121_mod_chat_feedback
Revises: 20261121_mod_auth_phone_otp
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261121_mod_chat_feedback"
down_revision: Union[str, None] = "20261121_mod_auth_phone_otp"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_reaction",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=True),
        sa.Column("anonymous_sid", sa.Text(), nullable=True),
        sa.Column("reaction", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("reaction IN ('POSITIVE','NEGATIVE')", name="ck_message_reaction_value"),
        sa.CheckConstraint(
            "customer_id IS NOT NULL OR anonymous_sid IS NOT NULL", name="ck_message_reaction_owner"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["message.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # UNIQUE NULLS NOT DISTINCT: una reacción por mensaje y por quien reacciona.
    op.create_index(
        "uq_message_reaction_message_owner",
        "message_reaction",
        ["message_id", "customer_id", "anonymous_sid"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index("ix_message_reaction_message_id", "message_reaction", ["message_id"], unique=False)

    op.create_table(
        "feedback_suggestion",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.Text(), nullable=False),
        sa.Column("screenshot_key", sa.Text(), nullable=True),
        sa.Column("notification_status", sa.Text(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("type IN ('SUGGESTION','BUG')", name="ck_feedback_suggestion_type"),
        sa.CheckConstraint("char_length(title) BETWEEN 3 AND 120", name="ck_feedback_suggestion_title"),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 6 AND 1000",
            name="ck_feedback_suggestion_description",
        ),
        sa.CheckConstraint(
            "notification_status IN ('PENDING','SENT','FAILED')",
            name="ck_feedback_suggestion_notification",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_feedback_suggestion_customer_created",
        "feedback_suggestion",
        ["customer_id", sa.text("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "purchase_rating",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_purchase_rating_value"),
        sa.CheckConstraint(
            "comment IS NULL OR char_length(comment) <= 500", name="ck_purchase_rating_comment"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
    )

    op.add_column(
        "order_ref",
        sa.Column("csat_sent", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.drop_constraint("ck_outbox_type", "outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_type",
        "outbox",
        "type IN ('NOTIFY_SALES_PAYMENT','REQUEST_CANCELLATION','SEND_EMAIL','SEND_FEEDBACK_EMAIL')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_type", "outbox", type_="check")
    op.create_check_constraint(
        "ck_outbox_type",
        "outbox",
        "type IN ('NOTIFY_SALES_PAYMENT','REQUEST_CANCELLATION','SEND_EMAIL')",
    )
    op.drop_column("order_ref", "csat_sent")
    op.drop_table("purchase_rating")
    op.drop_index("ix_feedback_suggestion_customer_created", table_name="feedback_suggestion")
    op.drop_table("feedback_suggestion")
    op.drop_index("ix_message_reaction_message_id", table_name="message_reaction")
    op.drop_index("uq_message_reaction_message_owner", table_name="message_reaction")
    op.drop_table("message_reaction")
