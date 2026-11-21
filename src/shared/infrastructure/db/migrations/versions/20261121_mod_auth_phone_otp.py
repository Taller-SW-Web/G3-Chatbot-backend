"""Phone OTP challenge table (SPEC-04).

Revision ID: 20261121_mod_auth_phone_otp
Revises: 0001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261121_mod_auth_phone_otp"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "phone_otp_challenge",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(r"phone ~ '^\+51[0-9]{9}$'", name="ck_phone_otp_challenge_format"),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="ck_phone_otp_challenge_attempts"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_phone_otp_challenge_customer_created_at",
        "phone_otp_challenge",
        ["customer_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_phone_otp_challenge_customer_phone",
        "phone_otp_challenge",
        ["customer_id", "phone"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_phone_otp_challenge_customer_phone", table_name="phone_otp_challenge")
    op.drop_index("ix_phone_otp_challenge_customer_created_at", table_name="phone_otp_challenge")
    op.drop_table("phone_otp_challenge")
