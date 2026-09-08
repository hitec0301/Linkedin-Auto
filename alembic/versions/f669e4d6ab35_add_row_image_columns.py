"""add row image columns

Revision ID: f669e4d6ab35
Revises: 59106260892b
Create Date: 2026-08-25T00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f669e4d6ab35'
down_revision: Union[str, None] = '59106260892b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pipeline_rows', sa.Column('image_prompt', sa.Text(), nullable=False, server_default=''))
    op.add_column('pipeline_rows', sa.Column('image_data', sa.Text(), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('pipeline_rows', 'image_data')
    op.drop_column('pipeline_rows', 'image_prompt')
