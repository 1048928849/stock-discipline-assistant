"""Link research caches to exact quality records.

Revision ID: 20260725_0011
Revises: 20260724_0010
"""

from alembic import op
import sqlalchemy as sa


revision = "20260725_0011"
down_revision = "20260724_0010"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_column(table: str) -> bool:
    return any(
        column["name"] == "quality_record_id"
        for column in _inspector().get_columns(table)
    )


def _has_fk(table: str) -> bool:
    return any(
        item.get("constrained_columns") == ["quality_record_id"]
        and item.get("referred_table") == "data_quality_records"
        for item in _inspector().get_foreign_keys(table)
    )


def _fk_name(table: str) -> str | None:
    return next(
        (
            item.get("name")
            for item in _inspector().get_foreign_keys(table)
            if item.get("constrained_columns") == ["quality_record_id"]
            and item.get("referred_table") == "data_quality_records"
        ),
        None,
    )


def _has_index(table: str, name: str) -> bool:
    return any(item["name"] == name for item in _inspector().get_indexes(table))


def upgrade() -> None:
    for table, fk_name, index_name in (
        (
            "company_profiles",
            "fk_company_profiles_quality_record",
            "ix_company_profiles_quality_record_id",
        ),
        (
            "company_research_refreshes",
            "fk_company_research_refreshes_quality_record",
            "ix_company_research_refreshes_quality_record_id",
        ),
    ):
        if not _has_column(table):
            with op.batch_alter_table(table) as batch:
                batch.add_column(
                    sa.Column("quality_record_id", sa.Integer(), nullable=True)
                )
        if not _has_fk(table):
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(
                    fk_name,
                    "data_quality_records",
                    ["quality_record_id"],
                    ["id"],
                )
        if not _has_index(table, index_name):
            op.create_index(index_name, table, ["quality_record_id"])


def downgrade() -> None:
    for table, _, index_name in (
        (
            "company_research_refreshes",
            "fk_company_research_refreshes_quality_record",
            "ix_company_research_refreshes_quality_record_id",
        ),
        (
            "company_profiles",
            "fk_company_profiles_quality_record",
            "ix_company_profiles_quality_record_id",
        ),
    ):
        if _has_column(table):
            existing_fk_name = _fk_name(table)
            if existing_fk_name:
                with op.batch_alter_table(table) as batch:
                    batch.drop_constraint(existing_fk_name, type_="foreignkey")
        if _has_index(table, index_name):
            op.drop_index(index_name, table_name=table)
        if _has_column(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("quality_record_id")
