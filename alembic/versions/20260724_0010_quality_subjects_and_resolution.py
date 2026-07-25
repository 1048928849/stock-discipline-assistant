"""Add subject-scoped quality lineage and generation heads.

Revision ID: 20260724_0010
Revises: 20260724_0009
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME


revision = "20260724_0010"
down_revision = "20260724_0009"
branch_labels = None
depends_on = None


PRECISE_DATETIME = sa.DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {item["name"] for item in _inspector().get_indexes(table)}


def _constraint_names(table: str) -> set[str]:
    inspector = _inspector()
    names = {
        item["name"]
        for item in inspector.get_unique_constraints(table)
        if item.get("name")
    }
    names.update(
        item["name"]
        for item in inspector.get_foreign_keys(table)
        if item.get("name")
    )
    return names


def _has_foreign_key(
    table: str,
    constrained_columns: list[str],
    referred_table: str,
    referred_columns: list[str],
) -> bool:
    return any(
        item.get("constrained_columns") == constrained_columns
        and item.get("referred_table") == referred_table
        and item.get("referred_columns") == referred_columns
        for item in _inspector().get_foreign_keys(table)
    )


def _backfill_subjects() -> None:
    connection = op.get_bind()
    quality = sa.table(
        "data_quality_records",
        sa.column("id", sa.Integer()),
        sa.column("symbol", sa.String()),
        sa.column("subject_type", sa.String()),
        sa.column("subject_id", sa.String()),
    )
    rows = connection.execute(
        sa.select(quality.c.id, quality.c.symbol).where(
            quality.c.subject_type.is_(None),
            quality.c.subject_id.is_(None),
        )
    )
    for record_id, raw_symbol in rows:
        symbol = str(raw_symbol).strip() if raw_symbol is not None else ""
        if len(symbol) == 6 and symbol.isdigit():
            subject_type, subject_id = "stock", symbol
        elif symbol.lower() == "csi000300":
            subject_type, subject_id = "index", "CSI000300"
        else:
            continue
        connection.execute(
            quality.update()
            .where(quality.c.id == record_id)
            .values(subject_type=subject_type, subject_id=subject_id)
        )


def upgrade() -> None:
    existing_columns = _columns("data_quality_records")
    additions = [
        sa.Column("subject_type", sa.String(20)),
        sa.Column("subject_id", sa.String(160)),
        sa.Column("semantic_key", sa.String(200)),
        sa.Column("supersedes_record_id", sa.Integer()),
    ]
    missing = [column for column in additions if column.name not in existing_columns]
    if missing:
        with op.batch_alter_table("data_quality_records") as batch:
            for column in missing:
                batch.add_column(column)

    if not _has_foreign_key(
        "data_quality_records",
        ["supersedes_record_id"],
        "data_quality_records",
        ["id"],
    ):
        with op.batch_alter_table("data_quality_records") as batch:
            batch.create_foreign_key(
                "fk_data_quality_records_supersedes_record",
                "data_quality_records",
                ["supersedes_record_id"],
                ["id"],
            )

    if "ix_data_quality_records_subject_scope" not in _index_names(
        "data_quality_records"
    ):
        op.create_index(
            "ix_data_quality_records_subject_scope",
            "data_quality_records",
            ["capability", "subject_type", "subject_id", "semantic_key", "id"],
        )
    if "ix_data_quality_records_supersedes_record_id" not in _index_names(
        "data_quality_records"
    ):
        op.create_index(
            "ix_data_quality_records_supersedes_record_id",
            "data_quality_records",
            ["supersedes_record_id"],
        )

    _backfill_subjects()

    if not _inspector().has_table("data_quality_subject_heads"):
        op.create_table(
            "data_quality_subject_heads",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("capability", sa.String(80), nullable=False),
            sa.Column("subject_type", sa.String(20), nullable=False),
            sa.Column("subject_id", sa.String(160), nullable=False),
            sa.Column(
                "semantic_key",
                sa.String(200),
                nullable=False,
                server_default="",
            ),
            sa.Column("current_record_id", sa.Integer(), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "updated_at",
                PRECISE_DATETIME,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["current_record_id"],
                ["data_quality_records.id"],
                name="fk_quality_subject_head_current_record",
            ),
            sa.UniqueConstraint(
                "capability",
                "subject_type",
                "subject_id",
                "semantic_key",
                name="uq_data_quality_subject_head_scope",
            ),
        )
    if "ix_data_quality_subject_heads_current_record_id" not in _index_names(
        "data_quality_subject_heads"
    ):
        op.create_index(
            "ix_data_quality_subject_heads_current_record_id",
            "data_quality_subject_heads",
            ["current_record_id"],
        )
    if "ix_data_quality_subject_heads_scope_generation" not in _index_names(
        "data_quality_subject_heads"
    ):
        op.create_index(
            "ix_data_quality_subject_heads_scope_generation",
            "data_quality_subject_heads",
            ["capability", "subject_type", "subject_id", "semantic_key", "generation"],
        )


def downgrade() -> None:
    if _inspector().has_table("data_quality_subject_heads"):
        op.drop_table("data_quality_subject_heads")

    constraints = _constraint_names("data_quality_records")
    if "fk_data_quality_records_supersedes_record" in constraints:
        with op.batch_alter_table("data_quality_records") as batch:
            batch.drop_constraint(
                "fk_data_quality_records_supersedes_record",
                type_="foreignkey",
            )

    if "ix_data_quality_records_supersedes_record_id" in _index_names(
        "data_quality_records"
    ):
        op.drop_index(
            "ix_data_quality_records_supersedes_record_id",
            table_name="data_quality_records",
        )
    if "ix_data_quality_records_subject_scope" in _index_names(
        "data_quality_records"
    ):
        op.drop_index(
            "ix_data_quality_records_subject_scope",
            table_name="data_quality_records",
        )

    with op.batch_alter_table("data_quality_records") as batch:
        for column in (
            "supersedes_record_id",
            "semantic_key",
            "subject_id",
            "subject_type",
        ):
            if column in _columns("data_quality_records"):
                batch.drop_column(column)
