"""add discussion pasted_text

Revision ID: 069f4fc3fc6b
Revises: f669e4d6ab35
Create Date: 2026-09-14T00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '069f4fc3fc6b'
down_revision: Union[str, None] = 'f669e4d6ab35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('discussions', sa.Column('pasted_text', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('discussions', 'pasted_text')
