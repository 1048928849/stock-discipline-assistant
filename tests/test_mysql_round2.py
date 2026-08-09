from __future__ import annotations

import os
import re
import subprocess
import sys
import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.data_hub.contracts import DataProvider, ProviderMetadata
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    market_storage_naive_to_aware,
    utc_storage_naive_to_aware,
)
from app.domain.quality_subject import canonical_semantic_key
from app.errors import AppError
from app.models import (
    Account,
    CompanyAnnouncement,
    CompanyProfile,
    CompanyResearchRefresh,
    DataQualityRecord,
    DataQualitySubjectHead,
    PlanAnalysisRun,
    TradePlan,
)
from app.services.research_cache import (
    persist_announcement_catalog,
    persist_company_profile,
)
from app.schemas_workflow import OneClickPlanRequest
from app.services.one_click_pipeline import confirm_one_click_plan, run_one_click_analysis
from app.services.url_normalization import normalize_announcement_url
from app.services.workflow import ensure_default_rule_version


pytestmark = pytest.mark.mysql_integration
_TEST_DATABASE_NAME = re.compile(r"^stock_discipline_test_[0-9A-Za-z_]+$")
_PRODUCT_TABLES = (
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
)
_CANDIDATE_DISCOVERY_TABLES = (
    "industry_capital_flow_snapshots",
    "market_event_pool_snapshots",
    "candidate_discovery_runs",
    "candidate_industry_assessments",
    "discovery_candidates",
)
_HISTORY_BOOTSTRAP_TABLES = (
    "historical_data_bootstrap_runs",
    "historical_data_bootstrap_items",
)


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".replace(
        os.getenv("MYSQL_TEST_DATABASE_URL", "__not_configured__"),
        "<redacted-mysql-url>",
    )


def _admin_url(url: URL) -> URL:
    return url.set(database="mysql")


def _recreate_database(url: URL) -> None:
    database = url.database or ""
    if not _TEST_DATABASE_NAME.fullmatch(database):
        raise AssertionError("MYSQL_TEST_DATABASE_URL must use stock_discipline_test_*")
    engine = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
            connection.exec_driver_sql(
                f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci"
            )
    finally:
        engine.dispose()


def _drop_database(url: URL) -> None:
    database = url.database or ""
    if not _TEST_DATABASE_NAME.fullmatch(database):
        raise AssertionError("refusing to drop a non-test database")
    engine = create_engine(_admin_url(url), isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
    finally:
        engine.dispose()


def _alembic(url: URL, *arguments: str, succeeds: bool = True) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = url.render_as_string(hide_password=False)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=os.getcwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if succeeds and completed.returncode != 0:
        output = (completed.stdout + completed.stderr).replace(
            url.render_as_string(hide_password=False), "<redacted-mysql-url>"
        )
        pytest.fail(f"Alembic {' '.join(arguments)} failed:\n{output}")
    if not succeeds and completed.returncode == 0:
        pytest.fail(f"Alembic {' '.join(arguments)} unexpectedly succeeded")
    return completed


@pytest.fixture(scope="session")
def mysql_test_url() -> URL:
    configured = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not configured:
        pytest.skip("MYSQL_TEST_DATABASE_URL is not configured")
    url = make_url(configured)
    if not url.get_backend_name().startswith("mysql"):
        pytest.fail("MYSQL_TEST_DATABASE_URL must use a MySQL driver")
    if not _TEST_DATABASE_NAME.fullmatch(url.database or ""):
        pytest.fail("MYSQL_TEST_DATABASE_URL must point to stock_discipline_test_*")
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as connection:
            version, version_comment = connection.execute(
                text("SELECT VERSION(), @@version_comment")
            ).one()
            version = str(version)
            version_comment = str(version_comment)
            database_charset = connection.execute(
                text(
                    "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
                    "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"
                ),
                {"name": url.database},
            ).one()
            if not version.startswith("8.") or "mariadb" in (
                version + version_comment
            ).lower():
                pytest.fail(f"MySQL 8 is required; server reported {version}")
            if database_charset[0] != "utf8mb4":
                pytest.fail("isolated MySQL database must use utf8mb4")
        engine.dispose()
    except Exception as exc:
        pytest.fail(f"isolated MySQL connection failed: {_safe_error(exc)}")
    yield url
    _drop_database(url)


@pytest.fixture()
def mysql_database(mysql_test_url: URL) -> URL:
    _recreate_database(mysql_test_url)
    return mysql_test_url


@pytest.fixture()
def mysql_head_url(mysql_database: URL) -> URL:
    _alembic(mysql_database, "upgrade", "head")
    return mysql_database


@pytest.fixture()
def mysql_session_factory(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


class MySQLResearchProvider(DataProvider):
    def __init__(self, *, profile_name: str = "测试公司", announcements=None):
        self.metadata = ProviderMetadata(
            provider_id="mysql-research",
            supported_capabilities=("fundamental.profile", "announcement.catalog"),
            priority=1,
        )
        self.profile_name = profile_name
        self.announcements = announcements

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def company_profile(self, symbol):
        return {"name": self.profile_name, "industry": "数据库测试"}

    def company_announcements(self, symbol, start, end):
        return self.announcements if self.announcements is not None else []


class BarrierProfileProvider(MySQLResearchProvider):
    def __init__(self, barrier: Barrier):
        super().__init__()
        self.barrier = barrier

    def company_profile(self, symbol):
        self.barrier.wait(timeout=15)
        return super().company_profile(symbol)


def _router(session: Session, provider: DataProvider | None = None) -> DataHubRouter:
    registry = ProviderRegistry()
    registry.register(provider or MySQLResearchProvider())
    return DataHubRouter(session, registry)


def _announcement_row(index: int, published: date, *, url: str | None = None) -> dict:
    return {
        "公告标题": f"公告 {index}",
        "公告日期": published.isoformat(),
        "公告链接": url or f"https://example.test/announcement-{index}",
        "目录来源": "mysql-test",
    }


def _persist_catalog(
    session: Session,
    rows: list[dict],
    start: date,
    end: date,
    *,
    provider_id: str = "mysql-research",
):
    provider = MySQLResearchProvider(announcements=rows)
    provider.metadata = ProviderMetadata(
        provider_id=provider_id,
        supported_capabilities=("fundamental.profile", "announcement.catalog"),
        priority=1,
    )
    router = _router(session, provider)
    result = router.company_announcements("300502", start, end)
    refresh = persist_announcement_catalog(
        session, router, result, start=start, end=end
    )
    return router, result, refresh


def _quality_record(symbol: str, capability: str) -> DataQualityRecord:
    now = datetime(2026, 7, 25, 1, 30)
    return DataQualityRecord(
        symbol=symbol,
        capability=capability,
        quality_status="SINGLE_SOURCE",
        observed_at=now,
        fetched_at=now,
        provider_id="mysql-test",
        provider_observations=[{"source": "测试"}],
        conflict_fields=[],
        row_count=1,
        trusted=True,
        persisted=False,
    )


@lru_cache(maxsize=1)
def _pipeline_helpers():
    path = Path(__file__).with_name("test_one_click_pipeline.py")
    spec = importlib.util.spec_from_file_location("_mysql_pipeline_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load one-click test helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_mysql_analysis_runs(
    SessionLocal,
    monkeypatch,
    *,
    run_count: int,
) -> tuple[int, list[int]]:
    helpers = _pipeline_helpers()
    helpers.patch_benchmarks(monkeypatch)
    with SessionLocal() as session:
        account = Account(
            name="mysql-concurrency",
            total_assets=Decimal("300000"),
            cash=Decimal("300000"),
            available_cash=Decimal("300000"),
        )
        session.add(account)
        session.commit()
        helpers.seed_pattern(session)
        helpers.seed_profile(session)
        run_ids = []
        for _ in range(run_count):
            result = run_one_click_analysis(
                session,
                OneClickPlanRequest(
                    symbol="300502",
                    position_mode="空仓",
                    account_id=account.id,
                    enable_ai=False,
                ),
            )
            assert result["decision_package"]["freeze_allowed"] is True, result[
                "decision_package"
            ]["blocked_reasons"]
            run_ids.append(result["run_id"])
        return account.id, run_ids


def _confirm_threads(SessionLocal, run_ids: list[int]) -> list[tuple]:
    barrier = Barrier(len(run_ids))
    lock = Lock()
    outcomes = []

    def worker(run_id: int):
        with SessionLocal() as session:
            try:
                barrier.wait(timeout=20)
                saved = confirm_one_click_plan(session, run_id)
                outcome = ("ok", saved["id"], saved["plan_version"])
            except AppError as exc:
                outcome = ("error", exc.code)
            except Exception as exc:
                outcome = ("unexpected", type(exc).__name__, str(exc))
            with lock:
                outcomes.append(outcome)

    threads = [Thread(target=worker, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return outcomes


def test_mysql8_version_empty_upgrade_and_idempotency(mysql_database: URL):
    engine = create_engine(mysql_database, pool_pre_ping=True)
    with engine.connect() as connection:
        version, global_tz, session_tz = connection.execute(
            text("SELECT VERSION(), @@GLOBAL.time_zone, @@SESSION.time_zone")
        ).one()
    engine.dispose()
    assert str(version).startswith("8.")
    assert global_tz
    assert session_tz
    _alembic(mysql_database, "upgrade", "head")
    current = _alembic(mysql_database, "current")
    assert "20260809_0019" in current.stdout
    heads = _alembic(mysql_database, "heads")
    assert "20260809_0019" in heads.stdout
    _alembic(mysql_database, "upgrade", "head")


def test_mysql_0012_product_tables_legacy_upgrade_round_trip(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "20260725_0011")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        for table in reversed(_PRODUCT_TABLES):
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table}`")
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
    _alembic(mysql_database, "upgrade", "head")
    inspector = inspect(engine)
    assert set(_PRODUCT_TABLES) <= set(inspector.get_table_names())
    intraday_constraints = {
        item["name"] for item in inspector.get_unique_constraints("market_intraday_bars")
    }
    assert "uq_intraday_symbol_start_adjustment" in intraday_constraints
    assert any(
        item["constrained_columns"] == ["quality_record_id"]
        and item["referred_table"] == "data_quality_records"
        for item in inspector.get_foreign_keys("market_intraday_bars")
    )
    _alembic(mysql_database, "downgrade", "20260725_0011")
    assert not set(_PRODUCT_TABLES) & set(inspect(engine).get_table_names())
    _alembic(mysql_database, "upgrade", "head")
    engine.dispose()


def test_mysql_0013_watchlist_tables_previous_head_round_trip(mysql_database: URL):
    watchlist_tables = (
        "monitoring_events",
        "reanalysis_runs",
        "reanalysis_requests",
        "watchlist_transitions",
        "watchlist_revisions",
        "watchlist_monitor_leases",
        "watchlist_items",
    )
    _alembic(mysql_database, "upgrade", "20260726_0012")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        for table in watchlist_tables:
            connection.exec_driver_sql(f"DROP TABLE IF EXISTS `{table}`")
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
    _alembic(mysql_database, "upgrade", "head")
    assert set(watchlist_tables) <= set(inspect(engine).get_table_names())
    _alembic(mysql_database, "downgrade", "20260726_0012")
    assert not set(watchlist_tables) & set(inspect(engine).get_table_names())
    _alembic(mysql_database, "upgrade", "head")
    engine.dispose()


def test_mysql_0014_watchlist_semantics_round_trip(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "head")
    _alembic(mysql_database, "downgrade", "20260727_0013")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    inspector = inspect(engine)
    assert "industry_analysis_snapshots" not in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("watchlist_items")}
    assert "invalidation_rule_specs" not in columns
    assert "industry_name" not in columns
    _alembic(mysql_database, "upgrade", "head")
    inspector = inspect(engine)
    assert "industry_analysis_snapshots" in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("watchlist_items")}
    assert {"invalidation_rule_specs", "industry_name"} <= columns
    engine.dispose()


def test_mysql_0015_candidate_discovery_round_trip(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "head")
    _alembic(mysql_database, "downgrade", "20260727_0014")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    assert not set(_CANDIDATE_DISCOVERY_TABLES) & set(
        inspect(engine).get_table_names()
    )
    _alembic(mysql_database, "upgrade", "head")
    inspector = inspect(engine)
    assert set(_CANDIDATE_DISCOVERY_TABLES) <= set(inspector.get_table_names())
    run_columns = {
        item["name"] for item in inspector.get_columns("candidate_discovery_runs")
    }
    assert {
        "total_constituents",
        "historical_data_ready",
        "historical_data_missing",
        "coverage_ratio",
    } <= run_columns
    run_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("candidate_discovery_runs")
    }
    candidate_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("discovery_candidates")
    }
    assert "uq_candidate_discovery_run_identity" in run_constraints
    assert "uq_discovery_candidate_run_symbol" in candidate_constraints
    engine.dispose()


def test_mysql_0016_history_bootstrap_round_trip(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "head")
    _alembic(mysql_database, "downgrade", "20260727_0015")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    assert not set(_HISTORY_BOOTSTRAP_TABLES) & set(
        inspect(engine).get_table_names()
    )
    _alembic(mysql_database, "upgrade", "head")
    inspector = inspect(engine)
    assert set(_HISTORY_BOOTSTRAP_TABLES) <= set(inspector.get_table_names())
    run_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints(
            "historical_data_bootstrap_runs"
        )
    }
    item_constraints = {
        item["name"]
        for item in inspector.get_unique_constraints(
            "historical_data_bootstrap_items"
        )
    }
    assert "uq_history_bootstrap_run_identity" in run_constraints
    assert "uq_history_bootstrap_item_scope" in item_constraints


def test_mysql_0017_bootstrap_adapter_identity_round_trip(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "20260728_0016")
    _alembic(mysql_database, "upgrade", "head")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    columns = {
        item["name"]: item for item in inspect(engine).get_columns(
            "historical_data_bootstrap_runs"
        )
    }
    assert columns["adapter_version"]["type"].length == 64
    _alembic(mysql_database, "downgrade", "20260728_0016")
    _alembic(mysql_database, "upgrade", "head")
    engine.dispose()


def test_mysql_watchlist_monitor_lease_is_atomic(mysql_head_url: URL):
    from app.watchlist.lease import acquire_monitor_lease, release_monitor_lease

    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 7, 27, 2, 0, tzinfo=timezone.utc)
    barrier = Barrier(3)
    outcomes = []
    result_lock = Lock()

    def worker(token: str) -> None:
        with factory() as db:
            barrier.wait()
            acquired = acquire_monitor_lease(
                db, owner_token=token, lease_seconds=60, now=now
            )
            with result_lock:
                outcomes.append((token, acquired))

    threads = [
        Thread(target=worker, args=(token,))
        for token in ("mysql-worker-a", "mysql-worker-b")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert sorted(acquired for _, acquired in outcomes) == [False, True]
    winner = next(token for token, acquired in outcomes if acquired)
    with factory() as first, factory() as second:
        assert release_monitor_lease(first, owner_token=winner)
        assert acquire_monitor_lease(
            second, owner_token="mysql-worker-b", lease_seconds=60, now=now
        )
    engine.dispose()


def test_mysql_legacy_0008_upgrade_preserves_data_and_backfills_subjects(
    mysql_database: URL,
):
    _alembic(mysql_database, "upgrade", "20260723_0008")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        session.add(
            CompanyAnnouncement(
                symbol="300502",
                title="旧公告",
                announcement_category="其他公告",
                risk_level="绿",
                published_date=date(2026, 7, 24),
                catalog_source="legacy",
                exchange="深交所",
                url="https://example.test/legacy",
                raw_data={"内容": "中文旧数据"},
                fetched_at=datetime(2026, 7, 24, 1, 0),
            )
        )
        records = [
            _quality_record("300502", "market.daily.qfq"),
            _quality_record("csi000300", "market.index_daily"),
            _quality_record("CSI000300", "market.index_daily"),
            _quality_record("电子行业", "market.sector_daily"),
        ]
        session.add_all(records)
        session.commit()
        ids = [record.id for record in records]
    _alembic(mysql_database, "upgrade", "head")
    with SessionLocal() as session:
        rows = [session.get(DataQualityRecord, record_id) for record_id in ids]
        assert (rows[0].subject_type, rows[0].subject_id) == ("stock", "300502")
        assert (rows[1].subject_type, rows[1].subject_id) == ("index", "CSI000300")
        assert (rows[2].subject_type, rows[2].subject_id) == ("index", "CSI000300")
        assert rows[3].subject_type is None and rows[3].subject_id is None
        stored = session.query(CompanyAnnouncement).one()
        assert stored.raw_data == {"内容": "中文旧数据"}
    engine.dispose()


def test_mysql_0010_0011_round_trip_and_head_to_0009(mysql_database: URL):
    _alembic(mysql_database, "upgrade", "20260724_0010")
    _alembic(mysql_database, "upgrade", "20260725_0011")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    assert "quality_record_id" in {
        item["name"] for item in inspect(engine).get_columns("company_profiles")
    }
    _alembic(mysql_database, "downgrade", "20260724_0010")
    assert "quality_record_id" not in {
        item["name"] for item in inspect(engine).get_columns("company_profiles")
    }
    _alembic(mysql_database, "upgrade", "head")
    _alembic(mysql_database, "downgrade", "20260724_0009")
    assert not inspect(engine).has_table("data_quality_subject_heads")
    _alembic(mysql_database, "upgrade", "head")
    assert inspect(engine).has_table("data_quality_subject_heads")
    engine.dispose()


def _foreign_key_pairs(inspector, table: str) -> set[tuple[tuple[str, ...], str]]:
    return {
        (tuple(item["constrained_columns"]), item["referred_table"])
        for item in inspector.get_foreign_keys(table)
    }


def test_mysql_schema_constraints_json_and_full_url_index(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    inspector = inspect(engine)
    quality_columns = {item["name"] for item in inspector.get_columns("data_quality_records")}
    assert quality_columns == {
        "id",
        "symbol",
        "capability",
        "subject_type",
        "subject_id",
        "semantic_key",
        "supersedes_record_id",
        "quality_status",
        "observed_at",
        "fetched_at",
        "cached_at",
        "provider_id",
        "provider_observations",
        "normalized_digest",
        "conflict_fields",
        "adjustment",
        "price_unit",
        "volume_unit",
        "row_count",
        "fallback_used",
        "cache_used",
        "trusted",
        "persisted",
        "scan_start",
        "scan_end",
        "checked_at",
        "latest_content_at",
        "created_at",
    }
    quality_indexes = {item["name"] for item in inspector.get_indexes("data_quality_records")}
    assert {
        "ix_data_quality_records_symbol",
        "ix_data_quality_records_capability",
        "ix_data_quality_records_quality_status",
        "ix_data_quality_records_trusted",
        "ix_data_quality_records_persisted",
        "ix_data_quality_records_subject_scope",
        "ix_data_quality_records_supersedes_record_id",
    } <= quality_indexes
    assert {
        "subject_type",
        "subject_id",
        "semantic_key",
        "supersedes_record_id",
        "provider_observations",
        "persisted",
    } <= quality_columns
    quality_fks = _foreign_key_pairs(inspector, "data_quality_records")
    assert (("supersedes_record_id",), "data_quality_records") in quality_fks
    head_uniques = {
        item["name"]: tuple(item["column_names"])
        for item in inspector.get_unique_constraints("data_quality_subject_heads")
    }
    assert head_uniques["uq_data_quality_subject_head_scope"] == (
        "capability",
        "subject_type",
        "subject_id",
        "semantic_key",
    )
    assert (("current_record_id",), "data_quality_records") in _foreign_key_pairs(
        inspector, "data_quality_subject_heads"
    )
    assert "ix_data_quality_subject_heads_scope_generation" in {
        item["name"] for item in inspector.get_indexes("data_quality_subject_heads")
    }
    expected_fks = {
        "market_quotes": ("quality_record_id",),
        "market_daily_bars": ("quality_record_id",),
        "company_profiles": ("quality_record_id",),
        "company_research_refreshes": ("quality_record_id",),
    }
    for table_name, columns in expected_fks.items():
        assert (columns, "data_quality_records") in _foreign_key_pairs(
            inspector, table_name
        )
    plan_uniques = {
        item["name"] for item in inspector.get_unique_constraints("trade_plans")
    }
    assert {
        "uq_trade_plan_analysis_run",
        "uq_trade_plan_account_symbol_version",
    } <= plan_uniques
    assert (("analysis_run_id",), "plan_analysis_runs") in _foreign_key_pairs(
        inspector, "trade_plans"
    )
    assert "ix_trade_plans_analysis_run_id" in {
        item["name"] for item in inspector.get_indexes("trade_plans")
    }
    with engine.connect() as connection:
        url_column = connection.execute(
            text(
                "SELECT DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, CHARACTER_SET_NAME, "
                "COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=:schema AND TABLE_NAME='company_announcements' "
                "AND COLUMN_NAME='url'"
            ),
            {"schema": mysql_head_url.database},
        ).one()
        index_rows = connection.execute(
            text(
                "SELECT COLUMN_NAME, SEQ_IN_INDEX, SUB_PART, NON_UNIQUE "
                "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=:schema "
                "AND TABLE_NAME='company_announcements' "
                "AND INDEX_NAME='uq_company_announcement_url' ORDER BY SEQ_IN_INDEX"
            ),
            {"schema": mysql_head_url.database},
        ).all()
    assert tuple(url_column) == ("varchar", 1000, "ascii", "ascii_bin")
    assert [row[0] for row in index_rows] == ["symbol", "url"]
    assert all(row[2] is None and row[3] == 0 for row in index_rows)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        record = _quality_record("300502", "announcement.catalog")
        record.subject_type = "stock"
        record.subject_id = "300502"
        record.semantic_key = "  公告/人民币  "
        session.add(record)
        session.flush()
        session.add(
            DataQualitySubjectHead(
                capability=record.capability,
                subject_type="stock",
                subject_id="300502",
                semantic_key="  公告/人民币  ",
                current_record_id=record.id,
                generation=1,
            )
        )
        session.commit()
        head = session.query(DataQualitySubjectHead).one()
        assert head.semantic_key == canonical_semantic_key("  公告/人民币  ")
        assert session.get(DataQualityRecord, record.id).provider_observations == [
            {"source": "测试"}
        ]
    engine.dispose()


def _announcement_values(symbol: str, url: str, index: int) -> dict:
    return {
        "symbol": symbol,
        "title": f"公告 {index}",
        "announcement_category": "其他公告",
        "risk_level": "绿",
        "published_date": date(2026, 7, 25),
        "catalog_source": "mysql-test",
        "exchange": "深交所",
        "url": url,
        "raw_data": {"原始地址": url, "中文": "保留"},
        "fetched_at": datetime(2026, 7, 25, 1, 30),
    }


def test_mysql_full_url_uniqueness_and_long_common_prefix(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    prefix = "https://example.test/" + "a" * 900
    first = prefix + "1"
    second = prefix + "2"
    exact_prefix = "https://example.test/"
    exact_1000 = exact_prefix + "b" * (1000 - len(exact_prefix))
    with SessionLocal() as session:
        session.add_all(
            [
                CompanyAnnouncement(**_announcement_values("300502", first, 1)),
                CompanyAnnouncement(**_announcement_values("300502", second, 2)),
                CompanyAnnouncement(**_announcement_values("300502", exact_1000, 3)),
                CompanyAnnouncement(**_announcement_values("600000", first, 4)),
            ]
        )
        session.commit()
        assert session.query(CompanyAnnouncement).count() == 4
        session.add(CompanyAnnouncement(**_announcement_values("300502", first, 5)))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    engine.dispose()


def test_mysql_unicode_url_persistence_keeps_raw_payload_and_digest(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    start, end = date(2026, 7, 1), date(2026, 7, 25)
    raw_url = "https://例子.测试/公告?q=你好"
    with SessionLocal() as session:
        _, result, _ = _persist_catalog(
            session, [_announcement_row(1, end, url=raw_url)], start, end
        )
        original_digest = result.normalized_digest
        session.commit()
        stored = session.query(CompanyAnnouncement).one()
        record = session.get(DataQualityRecord, result.quality_record_id)
        assert stored.url == normalize_announcement_url(raw_url)
        assert stored.raw_data["公告链接"] == raw_url
        assert result.normalized_digest == original_digest == record.normalized_digest
        assert record.persisted is True
    engine.dispose()


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://example.test/invalid",
        "https://example.test/" + "x" * 1000,
    ],
)
def test_mysql_invalid_url_preserves_catalog_and_unpersisted_lineage(
    mysql_head_url: URL,
    invalid_url: str,
):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    start, end = date(2026, 7, 1), date(2026, 7, 25)
    with SessionLocal() as session:
        _, old_result, old_refresh = _persist_catalog(
            session, [_announcement_row(1, end)], start, end, provider_id="old"
        )
        session.commit()
        old_url = session.query(CompanyAnnouncement).one().url
        old_refresh_id = old_refresh.quality_record_id
        router = _router(
            session,
            MySQLResearchProvider(
                announcements=[
                    _announcement_row(2, end),
                    _announcement_row(3, end, url=invalid_url),
                ]
            ),
        )
        result = router.company_announcements("300502", start, end)
        with pytest.raises(Exception, match="invalid announcement URL"):
            persist_announcement_catalog(session, router, result, start=start, end=end)
        session.commit()
        assert session.query(CompanyAnnouncement).one().url == old_url
        refresh = session.query(CompanyResearchRefresh).one()
        assert refresh.quality_record_id == old_refresh_id == old_result.quality_record_id
        assert session.get(DataQualityRecord, result.quality_record_id).persisted is False
    engine.dispose()


def test_mysql_profile_flush_failure_rolls_back_business_savepoint(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        router = _router(session, MySQLResearchProvider(profile_name="original"))
        original_result = router.company_profile("300502")
        persist_company_profile(session, router, original_result)
        session.commit()
        session.execute(
            text(
                "CREATE TRIGGER reject_profile_update BEFORE UPDATE ON company_profiles "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='profile failure'"
            )
        )
        replacement_router = _router(
            session, MySQLResearchProvider(profile_name="replacement")
        )
        replacement = replacement_router.company_profile("300502")
        with pytest.raises(DBAPIError):
            persist_company_profile(session, replacement_router, replacement)
        session.commit()
        session.expire_all()
        stored = session.query(CompanyProfile).one()
        assert stored.name == "original"
        assert stored.quality_record_id == original_result.quality_record_id
        assert session.get(DataQualityRecord, replacement.quality_record_id).persisted is False
    engine.dispose()


def test_mysql_mark_persisted_failure_rolls_back_profile(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        router = _router(session)
        result = router.company_profile("300502")
        session.execute(
            text(
                "CREATE TRIGGER reject_persisted_update BEFORE UPDATE ON data_quality_records "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='persist failure'"
            )
        )
        with pytest.raises(DBAPIError):
            persist_company_profile(session, router, result)
        session.commit()
        assert session.query(CompanyProfile).count() == 0
        assert session.get(DataQualityRecord, result.quality_record_id).persisted is False
    engine.dispose()


def test_mysql_announcement_insert_failure_restores_old_catalog(mysql_head_url: URL):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    start, end = date(2026, 7, 1), date(2026, 7, 25)
    collision = "https://example.test/collision"
    with SessionLocal() as session:
        _, old_result, old_refresh = _persist_catalog(
            session, [_announcement_row(1, end)], start, end, provider_id="old"
        )
        outside_values = _announcement_values("300502", collision, 99)
        outside_values["published_date"] = start - timedelta(days=1)
        session.add(CompanyAnnouncement(**outside_values))
        session.commit()
        router = _router(
            session,
            MySQLResearchProvider(
                announcements=[_announcement_row(2, end, url=collision)]
            ),
        )
        result = router.company_announcements("300502", start, end)
        with pytest.raises(IntegrityError):
            persist_announcement_catalog(session, router, result, start=start, end=end)
        session.commit()
        session.expire_all()
        urls = {row.url for row in session.query(CompanyAnnouncement).all()}
        assert urls == {"https://example.test/announcement-1", collision}
        assert session.query(CompanyResearchRefresh).one().quality_record_id == (
            old_refresh.quality_record_id
        )
        assert old_refresh.quality_record_id == old_result.quality_record_id
        assert session.get(DataQualityRecord, result.quality_record_id).persisted is False
    engine.dispose()


def test_mysql_subject_head_concurrent_advance_is_atomic(mysql_session_factory):
    barrier = Barrier(2)
    outcomes = []
    lock = Lock()

    def worker():
        with mysql_session_factory() as session:
            try:
                router = _router(session, BarrierProfileProvider(barrier))
                result = router.company_profile("300502")
                session.commit()
                outcome = ("ok", result.quality_record_id)
            except Exception as exc:
                session.rollback()
                outcome = ("error", type(exc).__name__)
            with lock:
                outcomes.append(outcome)

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert [item[0] for item in outcomes] == ["ok", "ok"]
    with mysql_session_factory() as session:
        heads = session.scalars(select(DataQualitySubjectHead)).all()
        assert len(heads) == 1
        assert heads[0].generation == 2
        assert session.get(DataQualityRecord, heads[0].current_record_id) is not None
        assert session.query(DataQualityRecord).count() == 2


def test_mysql_concurrent_confirm_same_run_is_idempotent_and_uses_for_update(
    mysql_session_factory,
    monkeypatch,
):
    _, run_ids = _seed_mysql_analysis_runs(
        mysql_session_factory, monkeypatch, run_count=1
    )
    statements = []
    statement_lock = Lock()

    def capture_for_update(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        del connection, cursor, parameters, context, executemany
        if "FOR UPDATE" in statement.upper():
            with statement_lock:
                statements.append(statement)

    engine = mysql_session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", capture_for_update)
    try:
        outcomes = _confirm_threads(
            mysql_session_factory, [run_ids[0], run_ids[0]]
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture_for_update)
    assert [item[0] for item in outcomes].count("ok") == 1, outcomes
    assert ("error", "ANALYSIS_ALREADY_CONFIRMED") in outcomes
    assert not any(item[0] == "unexpected" for item in outcomes)
    with mysql_session_factory() as session:
        assert session.query(TradePlan).count() == 1
        assert session.get(PlanAnalysisRun, run_ids[0]).confirmed_plan_id is not None
    normalized = [statement.lower() for statement in statements]
    assert any("from plan_analysis_runs" in statement for statement in normalized)
    assert any("from trade_plans" in statement for statement in normalized)


def test_mysql_concurrent_same_plan_version_maps_to_business_conflict(
    mysql_session_factory,
    monkeypatch,
):
    _, run_ids = _seed_mysql_analysis_runs(
        mysql_session_factory, monkeypatch, run_count=2
    )
    insert_barrier = Barrier(2)

    def synchronize_trade_plan_insert(
        connection,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        del connection, cursor, parameters, context, executemany
        if statement.lstrip().upper().startswith("INSERT INTO TRADE_PLANS"):
            insert_barrier.wait(timeout=20)

    engine = mysql_session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", synchronize_trade_plan_insert)
    try:
        outcomes = _confirm_threads(mysql_session_factory, run_ids)
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_trade_plan_insert)
    assert [item[0] for item in outcomes].count("ok") == 1, outcomes
    assert ("error", "PLAN_VERSION_CONFLICT") in outcomes
    assert not any(item[0] == "unexpected" for item in outcomes)
    with mysql_session_factory() as session:
        plans = session.scalars(select(TradePlan)).all()
        assert len(plans) == 1
        assert plans[0].plan_version == 1


@pytest.mark.parametrize("session_time_zone", ["+00:00", "+08:00"])
def test_mysql_datetime_round_trip_is_not_shifted(mysql_head_url: URL, session_time_zone):
    engine = create_engine(mysql_head_url, pool_pre_ping=True)
    stored = datetime(2026, 7, 25, 9, 30)
    with engine.begin() as connection:
        connection.execute(text("SET time_zone=:zone"), {"zone": session_time_zone})
        connection.execute(
            text(
                "INSERT INTO market_quotes "
                "(symbol, price, quote_type, observed_at, price_unit, quality_status, "
                "source, source_api, fetched_at) "
                "VALUES ('300502', 10.82, 'realtime', :value, 'CNY', "
                "'SINGLE_SOURCE', 'test', 'test', :value)"
            ),
            {"value": stored},
        )
        connection.execute(
            text(
                "INSERT INTO company_profiles "
                "(symbol, name, source, raw_data, fetched_at) "
                "VALUES ('300502', '测试公司', 'test', JSON_OBJECT('中文', '值'), :value)"
            ),
            {"value": stored},
        )
    with engine.connect() as connection:
        connection.execute(text("SET time_zone=:zone"), {"zone": session_time_zone})
        market_value = connection.scalar(
            text("SELECT observed_at FROM market_quotes WHERE symbol='300502'")
        )
        research_value = connection.scalar(
            text("SELECT fetched_at FROM company_profiles WHERE symbol='300502'")
        )
    assert market_value == stored
    assert research_value == stored
    assert market_storage_naive_to_aware(market_value).replace(tzinfo=None) == stored
    assert utc_storage_naive_to_aware(research_value).astimezone(timezone.utc).replace(
        tzinfo=None
    ) == stored
    engine.dispose()


def _minimal_trade_plan(
    account_id: int,
    rule_version_id: int,
    *,
    analysis_run_id: int | None,
    plan_version: int,
) -> TradePlan:
    return TradePlan(
        account_id=account_id,
        rule_version_id=rule_version_id,
        symbol="300502",
        name="测试公司",
        status="READY",
        trade_mode="cash",
        decision_level="日线",
        buy_zone_low=Decimal("10.4209"),
        buy_zone_high=Decimal("10.5391"),
        initial_stop=Decimal("9.7023"),
        invalidation_condition="test",
        account_equity=Decimal("300000"),
        risk_pct=Decimal("1"),
        max_position_pct=Decimal("30"),
        planned_quantity=600,
        planned_position_value=Decimal("6323.46"),
        planned_risk_amount=Decimal("77.77"),
        next_action="test",
        data_status="success",
        data_date=date(2026, 7, 25),
        source="test",
        plan_version=plan_version,
        analysis_run_id=analysis_run_id,
    )


@pytest.mark.parametrize(
    ("constraint_name", "duplicate_kind", "expected_columns"),
    [
        ("uq_trade_plan_analysis_run", "analysis", "analysis_run_id"),
        (
            "uq_trade_plan_account_symbol_version",
            "version",
            "account_id, symbol, plan_version",
        ),
    ],
)
def test_mysql_0009_duplicate_plan_upgrade_fails_actionably(
    mysql_database: URL,
    constraint_name: str,
    duplicate_kind: str,
    expected_columns: str,
):
    _alembic(mysql_database, "upgrade", "20260723_0008")
    engine = create_engine(mysql_database, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE trade_plans DROP INDEX `{constraint_name}`"
        )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    with SessionLocal() as session:
        account = Account(
            name="duplicate-test",
            total_assets=Decimal("300000"),
            cash=Decimal("300000"),
            available_cash=Decimal("300000"),
        )
        session.add(account)
        session.flush()
        rule = ensure_default_rule_version(session)
        run = PlanAnalysisRun(
            symbol="300502",
            account_id=account.id,
            position_mode="空仓",
            status="success",
            request_snapshot={},
            pipeline_steps=[],
            result_snapshot={},
        )
        session.add(run)
        session.flush()
        first_run_id = run.id if duplicate_kind == "analysis" else None
        session.add(
            _minimal_trade_plan(
                account.id,
                rule.id,
                analysis_run_id=first_run_id,
                plan_version=1,
            )
        )
        session.add(
            _minimal_trade_plan(
                account.id,
                rule.id,
                analysis_run_id=first_run_id,
                plan_version=2 if duplicate_kind == "analysis" else 1,
            )
        )
        session.commit()
    completed = _alembic(mysql_database, "upgrade", "head", succeeds=False)
    output = completed.stdout + completed.stderr
    assert constraint_name in output
    assert expected_columns in output
    assert "resolve duplicates without deleting plans" in output
    with SessionLocal() as session:
        assert session.query(TradePlan).count() == 2
    engine.dispose()
