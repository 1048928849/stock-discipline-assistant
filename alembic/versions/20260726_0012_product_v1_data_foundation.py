"""Add Product V1 market, industry, concept, and chain storage.

Revision ID: 20260726_0012
Revises: 20260725_0011
"""

from alembic import op

from app.database import Base
from app import models  # noqa: F401


revision = "20260726_0012"
down_revision = "20260725_0011"
branch_labels = None
depends_on = None


TABLE_NAMES = (
    "market_intraday_bars",
    "market_turnover_snapshots",
    "market_breadth_snapshots",
    "market_amount_snapshots",
    "industry_market_snapshots",
    "industry_constituent_snapshots",
    "concepts",
    "company_concepts",
    "industry_chains",
    "industry_chain_nodes",
    "company_chain_positions",
    "mapping_evidence",
)


def upgrade() -> None:
    bind = op.get_bind()
    # The repository's initial revision creates current Base.metadata for fresh
    # databases. checkfirst keeps that path idempotent while creating these
    # tables for databases that were already at 0011.
    for name in TABLE_NAMES:
        Base.metadata.tables[name].create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in reversed(TABLE_NAMES):
        Base.metadata.tables[name].drop(bind, checkfirst=True)
