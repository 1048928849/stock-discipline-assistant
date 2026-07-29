from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import get_trading_calendar, shanghai_now
from app.discovery.service import CandidateDiscoveryService
from app.history.contracts import HistoryRequirementPlan
from app.history.service import HistoricalDataBootstrapService
from app.models import HistoricalDataBootstrapItem
from app.providers.baostock_provider import BaoStockBenchmarkProvider
from app.providers.freestockdb import FreeStockDBProvider


pytestmark = pytest.mark.baostock_integration


def _provider() -> BaoStockBenchmarkProvider:
    return BaoStockBenchmarkProvider(
        Settings(
            baostock_enabled=True,
            baostock_worker_timeout_seconds=10,
            baostock_max_response_bytes=2_000_000,
            baostock_max_rows=500,
        )
    )


def test_real_baostock_csi300_contract_and_repeatability():
    provider = _provider()
    calendar = get_trading_calendar()
    end = calendar.latest_completed_session(shanghai_now())
    start = date(end.year, 1, 1)

    assert provider.health_check(probe=True)["status"] == "READY"
    results = [
        provider.get_index_history("CSI000300", start, end)
        for _ in range(3)
    ]

    digests = []
    for result in results:
        rows = result["rows"]
        dates = [row["date"] for row in rows]
        assert len(rows) >= 80
        assert dates == sorted(set(dates))
        assert dates[-1] == end
        assert all(row["code"] == "sh.000300" for row in rows)
        assert all(
            row["high"] >= max(row["open"], row["close"], row["low"])
            and row["low"] <= min(row["open"], row["close"], row["high"])
            and min(row["open"], row["high"], row["low"], row["close"])
            > Decimal("0")
            and row["volume"] >= 0
            and row["amount"] >= 0
            for row in rows
        )
        lineage = result["provider_lineage"]
        assert lineage["provider_id"] == "baostock-benchmark"
        assert lineage["process_isolation"] == "hard-timeout-subprocess"
        assert lineage["timeout_seconds"] == 10
        assert lineage["volume_unit"] == "share"
        assert lineage["amount_unit"] == "CNY"
        assert lineage["worker_protocol_version"] == "1.0.0"
        assert lineage["baostock_package_version"] == "00.9.30"
        digests.append(lineage["response_digest"])
    assert len(set(digests)) == 1
    assert provider.health_check()["status"] == "READY"


def _assert_real_history_cold_start(session, monkeypatch):
    import test_candidate_discovery_cold_start as cold_start

    current = shanghai_now()
    calendar = get_trading_calendar()
    end = calendar.latest_completed_session(current)
    dates = []
    cursor = end
    while len(dates) < 80:
        if calendar.is_session(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    start = dates[-1]
    monkeypatch.setattr(cold_start, "NOW", current)
    monkeypatch.setattr(cold_start, "DAY", end)
    cold_start._seed_analysis_context(session)

    settings = Settings(
        freestockdb_enabled=True,
        freestockdb_base_url="http://127.0.0.1:7899",
        freestockdb_history_lookback_sessions=80,
        freestockdb_refresh_rewrite_sessions=80,
        baostock_enabled=True,
        baostock_worker_timeout_seconds=10,
        baostock_max_response_bytes=2_000_000,
        baostock_max_rows=500,
        candidate_min_history_coverage_ratio=Decimal("0.8"),
        candidate_discovery_max_industries=3,
        candidate_discovery_max_per_industry=10,
        candidate_discovery_max_candidates=30,
    )
    discovery_registry = ProviderRegistry()
    discovery_registry.register(
        cold_start.DiscoveryFoundationProvider(symbols=("600519",))
    )
    discovery_router = DataHubRouter(
        session, discovery_registry, calendar=calendar, now_fn=lambda: current
    )
    history_registry = ProviderRegistry()
    history_registry.register(
        BaoStockBenchmarkProvider(settings, calendar=calendar)
    )
    history_registry.register(
        FreeStockDBProvider(settings, calendar=calendar)
    )
    history_router = DataHubRouter(session, history_registry, calendar=calendar)
    plan = HistoryRequirementPlan.create(
        trade_date=end,
        benchmark_symbols=("CSI000300",),
        selected_industries=(cold_start.INDUSTRY,),
        required_stock_symbols=("600519",),
        required_capabilities=(
            "market.daily.qfq",
            "market.index_daily",
            "market.turnover.daily",
        ),
        start_date=start,
        end_date=end,
        minimum_rows=80,
        config={"integration": "real-history"},
    )
    bootstrap_service = HistoricalDataBootstrapService(
        session,
        router=history_router,
        planning_router=history_router,
        settings=settings,
        plan_factory=lambda **kwargs: plan,
        now_fn=lambda: current,
    )

    bootstrap = bootstrap_service.run(trade_date=end, now=current)
    repeated_bootstrap = bootstrap_service.run(trade_date=end, now=current)

    item_failures = [
        (item.symbol, item.capability, item.status, item.error_code, item.error_message)
        for item in session.query(HistoricalDataBootstrapItem)
        .filter(HistoricalDataBootstrapItem.run_id == bootstrap.id)
        .all()
    ]
    assert bootstrap.status == "SUCCEEDED", (bootstrap.blocked_reasons, item_failures)
    assert bootstrap.benchmark_ready is True
    assert bootstrap.coverage_ratio >= Decimal("0.8")
    assert bootstrap.blocked_reasons == []
    assert repeated_bootstrap.id == bootstrap.id

    discovery_service = CandidateDiscoveryService(
        session,
        router=discovery_router,
        settings=settings,
        calendar=calendar,
    )
    discovery = discovery_service.run(now=current)
    repeated_discovery = discovery_service.run(now=current)

    assert discovery.status == "COMPLETED"
    assert discovery.blocked_reasons == []
    assert discovery.total_constituents == 1
    assert discovery.historical_data_ready == 1
    assert discovery.historical_data_missing == 0
    assert discovery.coverage_ratio == Decimal("1")
    assert discovery.input_snapshot_hash
    assert repeated_discovery.id == discovery.id
    assert repeated_discovery.input_snapshot_hash == discovery.input_snapshot_hash


@pytest.mark.freestockdb_integration
def test_real_history_cold_start_completes_bootstrap_and_discovery(
    tmp_path, monkeypatch
):
    root = Path(__file__).parents[1]
    database = tmp_path / "baostock-cold-start.db"
    database_url = f"sqlite:///{database.as_posix()}"
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert migration.returncode == 0, migration.stdout + migration.stderr
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session() as session:
            _assert_real_history_cold_start(session, monkeypatch)
    finally:
        engine.dispose()
