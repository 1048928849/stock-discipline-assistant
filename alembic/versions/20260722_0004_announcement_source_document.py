"""Add direct announcement source-document links.

Revision ID: 20260722_0004
Revises: 20260722_0003
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_0004"
down_revision = "20260722_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("company_announcements")}
    if "source_document_url" not in columns:
        op.add_column(
            "company_announcements",
            sa.Column("source_document_url", sa.String(1000), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("company_announcements")}
    if "source_document_url" in columns:
        op.drop_column("company_announcements", "source_document_url")
