from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
PRODUCT_TABLES = {
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
}


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _alembic(path: Path, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": _database_url(path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed_quality_record(path: Path, *, symbol: str | None, capability: str) -> int:
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO data_quality_records (
                symbol, capability, quality_status, observed_at, fetched_at,
                provider_id, provider_observations, conflict_fields, row_count,
                fallback_used, cache_used, trusted, persisted, created_at
            )
            VALUES (?, ?, 'SINGLE_SOURCE', '2026-07-24 14:00:00',
                    '2026-07-24 14:00:00', 'migration-test', '[]', '[]', 1,
                    0, 0, 1, 1, '2026-07-24 14:00:00')
            """,
            (symbol, capability),
        )
        connection.commit()
        return int(cursor.lastrowid)


def _quality_subject(path: Path, record_id: int) -> tuple[str | None, str | None]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT subject_type, subject_id
            FROM data_quality_records
            WHERE id = ?
            """,
            (record_id,),
        ).fetchone()
    assert row is not None
    return row


def test_0010_backfills_stock_subject(tmp_path):
    database = tmp_path / "stock-backfill.db"
    _alembic(database, "upgrade", "20260724_0009")
    record_id = _seed_quality_record(
        database,
        symbol="300502",
        capability="market.daily.qfq",
    )
    _alembic(database, "upgrade", "20260724_0010")
    assert _quality_subject(database, record_id) == ("stock", "300502")


def test_0010_normalizes_csi000300_subject(tmp_path):
    database = tmp_path / "index-backfill.db"
    _alembic(database, "upgrade", "20260724_0009")
    lower = _seed_quality_record(
        database,
        symbol="csi000300",
        capability="market.index_daily",
    )
    upper = _seed_quality_record(
        database,
        symbol="CSI000300",
        capability="market.index_daily",
    )
    _alembic(database, "upgrade", "20260724_0010")
    assert _quality_subject(database, lower) == ("index", "CSI000300")
    assert _quality_subject(database, upper) == ("index", "CSI000300")


def test_0010_does_not_guess_legacy_sector_subject(tmp_path):
    database = tmp_path / "sector-backfill.db"
    _alembic(database, "upgrade", "20260724_0009")
    record_id = _seed_quality_record(
        database,
        symbol=None,
        capability="market.sector_daily",
    )
    _alembic(database, "upgrade", "20260724_0010")
    assert _quality_subject(database, record_id) == (None, None)


def test_0010_downgrade_and_reupgrade(tmp_path):
    database = tmp_path / "roundtrip.db"
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"subject_type", "subject_id", "semantic_key", "supersedes_record_id"} <= columns
    assert "data_quality_subject_heads" in tables


def test_0011_research_lineage_roundtrip(tmp_path):
    database = tmp_path / "research-lineage-roundtrip.db"
    _alembic(database, "upgrade", "20260724_0010")
    _alembic(database, "upgrade", "20260725_0011")
    with sqlite3.connect(database) as connection:
        profile_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(company_profiles)")
        }
        refresh_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(company_research_refreshes)")
        }
        profile_fks = list(connection.execute("PRAGMA foreign_key_list(company_profiles)"))
        refresh_fks = list(
            connection.execute("PRAGMA foreign_key_list(company_research_refreshes)")
        )
    assert "quality_record_id" in profile_columns
    assert "quality_record_id" in refresh_columns
    assert any(row[2] == "data_quality_records" and row[3] == "quality_record_id" for row in profile_fks)
    assert any(row[2] == "data_quality_records" and row[3] == "quality_record_id" for row in refresh_fks)

    _alembic(database, "downgrade", "20260724_0010")
    with sqlite3.connect(database) as connection:
        assert "quality_record_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(company_profiles)")
        }
        assert "quality_record_id" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info(company_research_refreshes)")
        }
    _alembic(database, "upgrade", "head")

    _alembic(database, "downgrade", "20260724_0009")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "subject_type" not in columns
    assert "data_quality_subject_heads" not in tables

    _alembic(database, "upgrade", "20260724_0010")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"subject_type", "subject_id", "semantic_key", "supersedes_record_id"} <= columns
    assert "data_quality_subject_heads" in tables


def test_0012_product_tables_upgrade_and_downgrade(tmp_path):
    database = tmp_path / "product-data-roundtrip.db"
    _alembic(database, "upgrade", "20260725_0011")
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for table in PRODUCT_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.commit()
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        intraday_fks = list(
            connection.execute("PRAGMA foreign_key_list(market_intraday_bars)")
        )
    assert PRODUCT_TABLES <= tables
    assert any(
        row[2] == "data_quality_records" and row[3] == "quality_record_id"
        for row in intraday_fks
    )
    _alembic(database, "downgrade", "20260725_0011")
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert not PRODUCT_TABLES & remaining
    _alembic(database, "upgrade", "head")
