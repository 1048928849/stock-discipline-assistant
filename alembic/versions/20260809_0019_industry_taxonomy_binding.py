"""Add provider-native industry taxonomy bindings.

Revision ID: 20260809_0019
Revises: 20260731_0018
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME


revision = "20260809_0019"
down_revision = "20260731_0018"
branch_labels = None
depends_on = None

PRECISE_DATETIME = sa.DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")
TABLE_NAME = "industry_taxonomy_bindings"
EXPECTED_COLUMNS = {
    "id",
    "symbol",
    "classification_system",
    "provider_id",
    "provider_industry_id",
    "provider_industry_code",
    "provider_industry_name",
    "level",
    "effective_date",
    "observed_at",
    "fetched_at",
    "source_reference",
    "response_digest",
    "membership_evidence",
    "quality_record_id",
}


def _validate_metadata_table() -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE_NAME):
        return False
    columns = {item["name"] for item in inspector.get_columns(TABLE_NAME)}
    if columns != EXPECTED_COLUMNS:
        raise RuntimeError(f"existing {TABLE_NAME} columns do not match revision 0019")
    return True


def upgrade() -> None:
    if not _validate_metadata_table():
        op.create_table(
            TABLE_NAME,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("symbol", sa.String(length=12), nullable=False),
            sa.Column("classification_system", sa.String(length=40), nullable=False),
            sa.Column("provider_id", sa.String(length=80), nullable=False),
            sa.Column("provider_industry_id", sa.String(length=40), nullable=False),
            sa.Column("provider_industry_code", sa.String(length=40), nullable=True),
            sa.Column("provider_industry_name", sa.String(length=200), nullable=False),
            sa.Column("level", sa.String(length=40), nullable=False),
            sa.Column("effective_date", sa.Date(), nullable=False),
            sa.Column("observed_at", PRECISE_DATETIME, nullable=False),
            sa.Column("fetched_at", PRECISE_DATETIME, nullable=False),
            sa.Column("source_reference", sa.String(length=500), nullable=False),
            sa.Column("response_digest", sa.String(length=64), nullable=False),
            sa.Column("membership_evidence", sa.Text(), nullable=False),
            sa.Column("quality_record_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ["quality_record_id"], ["data_quality_records.id"]
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "quality_record_id",
                "provider_industry_id",
                name="uq_industry_taxonomy_binding_identity",
            ),
        )
        for column in (
            "symbol",
            "classification_system",
            "provider_id",
            "provider_industry_id",
            "provider_industry_name",
            "effective_date",
            "quality_record_id",
        ):
            op.create_index(
                f"ix_industry_taxonomy_bindings_{column}",
                TABLE_NAME,
                [column],
            )
    bootstrap_columns = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_columns("historical_data_bootstrap_runs")
    }
    if "amount_coverage_ratio" not in bootstrap_columns:
        op.add_column(
            "historical_data_bootstrap_runs",
            sa.Column(
                "amount_coverage_ratio",
                sa.Numeric(12, 6),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    op.drop_column("historical_data_bootstrap_runs", "amount_coverage_ratio")
    op.drop_table(TABLE_NAME)
