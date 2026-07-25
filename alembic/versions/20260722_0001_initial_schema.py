"""初始数据库结构。

Revision ID: 20260722_0001
Revises:
"""

from alembic import op

from app.database import Base
from app import models  # noqa: F401


revision = "20260722_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Logical schema comes from one metadata model; dialect-specific physical types may differ
    # when they preserve the same business semantics (for example, MySQL index byte limits).
    original_tables = [
        table for name, table in Base.metadata.tables.items() if name != "technical_snapshots"
    ]
    Base.metadata.create_all(bind=op.get_bind(), tables=original_tables)


def downgrade() -> None:
    original_tables = [
        table for name, table in Base.metadata.tables.items() if name != "technical_snapshots"
    ]
    Base.metadata.drop_all(bind=op.get_bind(), tables=original_tables)
