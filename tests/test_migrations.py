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
    "market_regime_snapshots",
    "concepts",
    "company_concepts",
    "industry_chains",
    "industry_chain_nodes",
    "company_chain_positions",
    "mapping_evidence",
}
WATCHLIST_TABLES = {
    "watchlist_items",
    "watchlist_revisions",
    "watchlist_transitions",
    "monitoring_events",
    "reanalysis_requests",
    "reanalysis_runs",
    "watchlist_monitor_leases",
}
CANDIDATE_DISCOVERY_TABLES = {
    "industry_capital_flow_snapshots",
    "market_event_pool_snapshots",
    "candidate_discovery_runs",
    "candidate_industry_assessments",
    "discovery_candidates",
}
HISTORY_BOOTSTRAP_TABLES = {
    "historical_data_bootstrap_runs",
    "historical_data_bootstrap_items",
}
SELECTED_STOCK_TABLES = {"selected_stock_analysis_runs"}
TRADING_DISCIPLINE_TABLES = {
    "trading_playbooks",
    "market_stage_snapshots",
    "source_evidence_records",
    "trade_thesis_snapshots",
    "pretrade_discipline_checks",
    "trading_training_programs",
    "trade_discipline_reviews",
}


def test_0020_trading_discipline_roundtrip(tmp_path):
    database = tmp_path / "trading-discipline-roundtrip.db"
    _alembic(database, "upgrade", "20260809_0019")
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        playbook = connection.execute(
            "SELECT code, version, active FROM trading_playbooks"
        ).fetchone()
    assert TRADING_DISCIPLINE_TABLES <= tables
    assert playbook == ("CORE_STATE_CHANGE_V1", "1.0.0", 1)
    _alembic(database, "downgrade", "20260809_0019")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not (TRADING_DISCIPLINE_TABLES & tables)
    _alembic(database, "upgrade", "head")


def test_0018_selected_stock_strategy_roundtrip(tmp_path):
    database = tmp_path / "selected-stock-strategy-roundtrip.db"
    _alembic(database, "upgrade", "20260728_0017")
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(selected_stock_analysis_runs)")
        }
    assert SELECTED_STOCK_TABLES <= tables
    assert "ix_selected_stock_symbol_date" in indexes
    assert any(indexes.values())

    _alembic(database, "downgrade", "20260728_0017")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not (SELECTED_STOCK_TABLES & tables)
    _alembic(database, "upgrade", "head")


def test_0018_uses_explicit_immutable_table_definitions():
    source = (ROOT / "alembic" / "versions" / "20260731_0018_selected_stock_strategy.py").read_text(
        encoding="utf-8"
    )
    assert "Base.metadata" not in source
    assert source.count("op.create_table") == len(SELECTED_STOCK_TABLES)


def test_0016_history_bootstrap_roundtrip(tmp_path):
    database = tmp_path / "history-bootstrap-roundtrip.db"
    _alembic(database, "upgrade", "20260727_0015")
    _alembic(database, "upgrade", "20260728_0016")
    with sqlite3.connect(database) as connection:
        upgraded = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        item_foreign_keys = list(
            connection.execute("PRAGMA foreign_key_list(historical_data_bootstrap_items)")
        )
    assert HISTORY_BOOTSTRAP_TABLES <= upgraded
    assert any(row[2] == "historical_data_bootstrap_runs" for row in item_foreign_keys)
    assert any(row[2] == "data_quality_records" for row in item_foreign_keys)
    _alembic(database, "downgrade", "20260727_0015")
    with sqlite3.connect(database) as connection:
        downgraded = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not (HISTORY_BOOTSTRAP_TABLES & downgraded)
    _alembic(database, "upgrade", "head")


def test_0017_expands_bootstrap_adapter_identity(tmp_path):
    database = tmp_path / "history-bootstrap-identity.db"
    _alembic(database, "upgrade", "20260728_0016")
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        columns = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA table_info(historical_data_bootstrap_runs)")
        }
    assert columns["adapter_version"] == "VARCHAR(64)"
    _alembic(database, "downgrade", "20260728_0016")
    _alembic(database, "upgrade", "head")


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


def test_0015_candidate_discovery_roundtrip(tmp_path):
    database = tmp_path / "candidate-discovery-roundtrip.db"
    _alembic(database, "upgrade", "20260727_0014")
    with sqlite3.connect(database) as connection:
        before = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    # Revision 0001 may create current metadata on a fresh database. Downgrade
    # establishes the historical 0014 shape before testing the explicit DDL.
    if CANDIDATE_DISCOVERY_TABLES <= before:
        _alembic(database, "upgrade", "20260727_0015")
        _alembic(database, "downgrade", "20260727_0014")
    _alembic(database, "upgrade", "20260727_0015")
    with sqlite3.connect(database) as connection:
        upgraded = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert CANDIDATE_DISCOVERY_TABLES <= upgraded
    with sqlite3.connect(database) as connection:
        run_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(candidate_discovery_runs)")
        }
    assert {
        "total_constituents",
        "historical_data_ready",
        "historical_data_missing",
        "coverage_ratio",
    } <= run_columns
    _alembic(database, "downgrade", "20260727_0014")
    with sqlite3.connect(database) as connection:
        downgraded = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not (CANDIDATE_DISCOVERY_TABLES & downgraded)
    _alembic(database, "upgrade", "head")


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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
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
            row[1] for row in connection.execute("PRAGMA table_info(company_research_refreshes)")
        }
        profile_fks = list(connection.execute("PRAGMA foreign_key_list(company_profiles)"))
        refresh_fks = list(
            connection.execute("PRAGMA foreign_key_list(company_research_refreshes)")
        )
    assert "quality_record_id" in profile_columns
    assert "quality_record_id" in refresh_columns
    assert any(
        row[2] == "data_quality_records" and row[3] == "quality_record_id" for row in profile_fks
    )
    assert any(
        row[2] == "data_quality_records" and row[3] == "quality_record_id" for row in refresh_fks
    )

    _alembic(database, "downgrade", "20260724_0010")
    with sqlite3.connect(database) as connection:
        assert "quality_record_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(company_profiles)")
        }
        assert "quality_record_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(company_research_refreshes)")
        }
    _alembic(database, "upgrade", "head")

    _alembic(database, "downgrade", "20260724_0009")
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "subject_type" not in columns
    assert "data_quality_subject_heads" not in tables

    _alembic(database, "upgrade", "20260724_0010")
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(data_quality_records)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
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
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        intraday_fks = list(connection.execute("PRAGMA foreign_key_list(market_intraday_bars)"))
    assert PRODUCT_TABLES <= tables
    assert any(
        row[2] == "data_quality_records" and row[3] == "quality_record_id" for row in intraday_fks
    )
    _alembic(database, "downgrade", "20260725_0011")
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not PRODUCT_TABLES & remaining
    _alembic(database, "upgrade", "head")


def test_0012_uses_explicit_immutable_table_definitions():
    source = (
        ROOT / "alembic" / "versions" / "20260726_0012_product_v1_data_foundation.py"
    ).read_text(encoding="utf-8")
    assert "Base.metadata" not in source
    assert "op.create_table" in source


def test_0013_watchlist_tables_upgrade_and_downgrade(tmp_path):
    database = tmp_path / "watchlist-roundtrip.db"
    _alembic(database, "upgrade", "20260726_0012")
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(monitoring_events)")
        }
    assert WATCHLIST_TABLES <= tables
    assert any(indexes.values())
    _alembic(database, "downgrade", "20260726_0012")
    with sqlite3.connect(database) as connection:
        remaining = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not WATCHLIST_TABLES & remaining
    _alembic(database, "upgrade", "head")


def test_0013_uses_explicit_immutable_table_definitions():
    source = (ROOT / "alembic" / "versions" / "20260727_0013_watchlist_monitoring.py").read_text(
        encoding="utf-8"
    )
    assert "Base.metadata" not in source
    assert source.count("op.create_table") == len(WATCHLIST_TABLES)


def test_0014_watchlist_semantics_downgrade_and_reupgrade(tmp_path):
    database = tmp_path / "watchlist-semantics-roundtrip.db"
    _alembic(database, "upgrade", "head")
    _alembic(database, "downgrade", "20260727_0013")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        watchlist_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(watchlist_items)")
        }
        regime_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(market_regime_snapshots)")
        }
    assert "industry_analysis_snapshots" not in tables
    assert "invalidation_rule_specs" not in watchlist_columns
    assert "industry_name" not in watchlist_columns
    assert "quality_bindings" not in regime_columns
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        watchlist_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(watchlist_items)")
        }
    assert "industry_analysis_snapshots" in tables
    assert {"invalidation_rule_specs", "industry_name"} <= watchlist_columns


def test_0014_uses_explicit_immutable_definitions():
    source = (
        ROOT / "alembic" / "versions" / "20260727_0014_watchlist_monitoring_semantics.py"
    ).read_text(encoding="utf-8")
    assert "Base.metadata" not in source
    assert "op.create_table" in source
