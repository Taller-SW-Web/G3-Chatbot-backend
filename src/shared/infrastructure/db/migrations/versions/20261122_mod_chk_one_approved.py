"""One approved payment attempt per checkout (BE-AUD-03).

Revision ID: 20261122_mod_chk_one_approved
Revises: 20261121_mod_chat_feedback
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20261122_mod_chk_one_approved"
down_revision: Union[str, None] = "20261121_mod_chat_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_payment_attempt_one_approved",
        "payment_attempt",
        ["checkout_id"],
        unique=True,
        postgresql_where=sa.text("result = 'APPROVED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_payment_attempt_one_approved", table_name="payment_attempt")
