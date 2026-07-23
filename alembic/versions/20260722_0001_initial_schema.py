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
    # 所有表均由同一份 SQLAlchemy 元数据定义，确保 SQLite/MySQL 使用完全相同的结构。
    original_tables = [
        table for name, table in Base.metadata.tables.items() if name != "technical_snapshots"
    ]
    Base.metadata.create_all(bind=op.get_bind(), tables=original_tables)


def downgrade() -> None:
    original_tables = [
        table for name, table in Base.metadata.tables.items() if name != "technical_snapshots"
    ]
    Base.metadata.drop_all(bind=op.get_bind(), tables=original_tables)
