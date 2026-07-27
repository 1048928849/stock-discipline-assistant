from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.config import Settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ, get_trading_calendar
from app.discovery.contracts import DiscoveryConfig, IndustryDiscoveryInput
from app.discovery.scoring import IndustryDiscoveryScorer
from app.history.contracts import HistoryRequirementPlan
from app.history.planning import HistoryRequirementPlanner
from app.history.service import HistoricalDataBootstrapService
from app.history.service import HistoryPlanningBlocked
from app.models import HistoricalDataBootstrapItem, HistoricalDataBootstrapRun
from app.providers.freestockdb import FreeStockDBProvider, FreeStockDBResponse


DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 19, 30, tzinfo=SHANGHAI_TZ)


def _industry(key: str, classification: str, flow: str) -> IndustryDiscoveryInput:
    return IndustryDiscoveryInput(
        industry_key=key,
        industry_name=key,
        classification=classification,
        relative_strength_5d=None,
        relative_strength_10d=None,
        relative_strength_20d=None,
        amount_share=Decimal("0.1"),
        advance_ratio=Decimal("0.7"),
        limit_up_count=2,
        leader_strength=Decimal("8"),
        new_high_ratio=Decimal("0.2"),
        net_inflow_1d=Decimal(flow),
        net_inflow_5d=Decimal(flow),
        net_inflow_10d=Decimal(flow),
        broken_limit_rate=Decimal("0.1"),
        quality_status="SINGLE_SOURCE",
        evidence_references=(f"industry:{key}",),
        total_constituents=0,
        historical_data_ready=0,
        historical_data_missing=0,
        coverage_ratio=Decimal("0"),
        constituents=(),
    )


def _sessions(count: int) -> list[date]:
    calendar = get_trading_calendar()
    result = []
    current = DAY
    while len(result) < count:
        if calendar.is_session(current):
            result.append(current)
        current -= timedelta(days=1)
    return sorted(result)


def test_history_requirement_plan_is_deterministic_and_reuses_formal_scorer():
    config = DiscoveryConfig(max_industries=2)
    industries = (
        _industry("A", "MAINLINE", "900000000"),
        _industry("B", "SECONDARY", "300000000"),
        _industry("C", "FADING", "-500000000"),
    )
    planner = HistoryRequirementPlanner(
        scorer=IndustryDiscoveryScorer(config),
        minimum_rows=50,
        lookback_sessions=80,
        rewrite_sessions=120,
        adapter_version="1.0.0",
    )
    kwargs = {
        "trade_date": DAY,
        "industries": industries,
        "constituents": {"A": ("600002", "600001"), "B": ("300001",), "C": ("000001",)},
        "calendar": get_trading_calendar(),
    }
    first = planner.build(**kwargs)
    second = planner.build(**kwargs)

    assert first == second
    assert first.plan_hash == second.plan_hash
    assert first.selected_industries == ("A", "B")
    assert first.required_stock_symbols == ("300001", "600001", "600002")
    assert first.required_capabilities == (
        "market.daily.qfq",
        "market.index_daily",
        "market.turnover.daily",
    )
    assert IndustryDiscoveryScorer(config).score(industries[0]) > (
        IndustryDiscoveryScorer(config).score(industries[2])
    )


class FixtureHistoryClient:
    def __init__(self, *, fail_once_symbol: str | None = None, rows: int = 20):
        self.fail_once_symbol = fail_once_symbol
        self.failed = False
        self.rows = rows
        self.calls = []

    def daily_history(self, **kwargs):
        symbol = kwargs["symbol"]
        self.calls.append((kwargs["kind"], symbol, kwargs["adjustment"]))
        if symbol == self.fail_once_symbol and not self.failed:
            self.failed = True
            raise ProviderUnavailableError("fixture symbol unavailable")
        days = _sessions(self.rows)
        payload = {
            "schema_version": "1.0",
            "data": {
                "symbol": symbol,
                "adjustment": kwargs["adjustment"],
                "price_unit": "CNY",
                "volume_unit": "share",
                "amount_unit": "CNY",
                "turnover_rate_unit": "percent",
                "rows": [
                    {
                        "trade_date": day.isoformat(),
                        "open": str(10 + index / 100),
                        "high": str(10.5 + index / 100),
                        "low": str(9.5 + index / 100),
                        "close": str(10.2 + index / 100),
                        "volume": "1000000",
                        "amount": "200000000",
                        "turnover_rate": "3",
                    }
                    for index, day in enumerate(days)
                ],
            },
        }
        return FreeStockDBResponse(
            payload=payload,
            raw_response_digest=("a" if kwargs["kind"] == "index" else "b") * 64,
            schema_version="1.0",
            source_url="http://127.0.0.1:7899/api/v1/history/daily",
        )

    def health(self):
        return {
            "schema_version": "1.0",
            "service": "free-stockdb",
            "version": "fixture",
            "status": "ok",
        }


def _plan(symbols=("600001",), *, minimum_rows=20):
    start = _sessions(minimum_rows)[0]
    return HistoryRequirementPlan.create(
        trade_date=DAY,
        benchmark_symbols=("CSI000300",),
        selected_industries=("A",),
        required_stock_symbols=tuple(symbols),
        required_capabilities=(
            "market.daily.qfq",
            "market.index_daily",
            "market.turnover.daily",
        ),
        start_date=start,
        end_date=DAY,
        minimum_rows=minimum_rows,
        config={"fixture": True},
    )


def _service(session, client, plan, **setting_updates):
    values = dict(
        freestockdb_enabled=True,
        freestockdb_csi300_symbol="fixture-csi300",
        freestockdb_history_lookback_sessions=plan.minimum_rows,
        freestockdb_refresh_rewrite_sessions=plan.minimum_rows,
        history_bootstrap_max_symbols=100,
        history_bootstrap_max_rows=10000,
        history_bootstrap_max_duration_seconds=60,
    )
    values.update(setting_updates)
    settings = Settings(**values)
    provider = FreeStockDBProvider(settings, client=client, now_fn=lambda: NOW)
    registry = ProviderRegistry()
    registry.register(provider)
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    return HistoricalDataBootstrapService(
        session,
        router=router,
        settings=settings,
        plan_factory=lambda **_kwargs: plan,
        now_fn=lambda: NOW,
    )


def test_bootstrap_success_is_idempotent_and_persists_progress(session):
    plan = _plan()
    client = FixtureHistoryClient(rows=20)
    service = _service(session, client, plan)

    first = service.run(trade_date=DAY)
    calls_after_first = list(client.calls)
    second = service.run(trade_date=DAY)

    assert first.id == second.id
    assert second.status == "SUCCEEDED"
    assert second.benchmark_ready is True
    assert second.ready_symbols == 1
    assert second.failed_symbols == 0
    assert second.coverage_ratio == Decimal("1")
    assert client.calls == calls_after_first
    items = session.scalars(
        select(HistoricalDataBootstrapItem)
        .where(HistoricalDataBootstrapItem.run_id == first.id)
        .order_by(HistoricalDataBootstrapItem.symbol, HistoricalDataBootstrapItem.capability)
    ).all()
    assert len(items) == 3
    assert {item.status for item in items} == {"SUCCEEDED"}
    assert all(item.quality_record_id for item in items)


def test_bootstrap_continues_after_symbol_failure_and_resumes_item(session):
    plan = _plan(("600001", "600002"))
    client = FixtureHistoryClient(fail_once_symbol="600001", rows=20)
    service = _service(session, client, plan)

    first = service.run(trade_date=DAY)
    assert first.status == "BLOCKED"
    assert first.ready_symbols == 1
    assert first.failed_symbols == 1

    second = service.run(trade_date=DAY)
    assert second.id == first.id
    assert second.status == "SUCCEEDED"
    assert second.ready_symbols == 2
    assert second.failed_symbols == 0
    assert session.scalar(select(HistoricalDataBootstrapRun).where(
        HistoricalDataBootstrapRun.id == first.id
    )).status == "SUCCEEDED"


def test_bootstrap_budget_blocks_without_silently_truncating_universe(session):
    plan = _plan(("600001", "600002"))
    service = _service(
        session,
        FixtureHistoryClient(rows=20),
        plan,
        history_bootstrap_max_symbols=1,
    )
    run = service.run(trade_date=DAY)
    assert run.status == "BLOCKED"
    assert run.required_symbols == ["CSI000300", "600001", "600002"]
    assert run.blocked_reasons == ["HISTORY_BOOTSTRAP_SYMBOL_BUDGET_EXCEEDED"]
    assert session.query(HistoricalDataBootstrapItem).count() == 0


def test_bootstrap_row_budget_blocks_without_creating_partial_items(session):
    plan = _plan(("600001",))
    service = _service(
        session,
        FixtureHistoryClient(rows=20),
        plan,
        history_bootstrap_max_rows=59,
    )
    run = service.run(trade_date=DAY)
    assert run.status == "BLOCKED"
    assert run.blocked_reasons == ["HISTORY_BOOTSTRAP_ROW_BUDGET_EXCEEDED"]
    assert session.query(HistoricalDataBootstrapItem).count() == 0


def test_planning_block_uses_formal_plan_hash_and_is_idempotent(session):
    plan = _plan()
    service = _service(session, FixtureHistoryClient(rows=20), plan)
    code = "INDUSTRY_PLANNING_DATA_NOT_EXECUTABLE"

    def blocked_plan(**_kwargs):
        raise HistoryPlanningBlocked(code)

    service.plan_factory = blocked_plan
    first = service.run(trade_date=DAY)
    second = service.run(trade_date=DAY)
    expected = HistoryRequirementPlan.create(
        trade_date=DAY,
        benchmark_symbols=(),
        selected_industries=(),
        required_stock_symbols=(),
        required_capabilities=(),
        start_date=DAY,
        end_date=DAY,
        minimum_rows=1,
        config={
            "planning_error": code,
            "adapter_version": service.settings.freestockdb_adapter_version,
        },
    )

    assert first.id == second.id
    assert first.config_hash == expected.config_hash
    assert first.plan_hash == expected.plan_hash


def test_duration_budget_blocks_and_same_run_can_resume(session, monkeypatch):
    plan = _plan(("600001", "600002"))
    service = _service(
        session,
        FixtureHistoryClient(rows=20),
        plan,
        history_bootstrap_max_duration_seconds=1,
    )
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr("app.history.service.time.monotonic", lambda: next(ticks))

    first = service.run(trade_date=DAY)

    assert first.status == "BLOCKED"
    assert first.ready_symbols == 0
    assert first.failed_symbols == 2
    assert "HISTORY_BOOTSTRAP_DURATION_BUDGET_EXCEEDED" in first.blocked_reasons

    monkeypatch.setattr("app.history.service.time.monotonic", lambda: 0.0)
    second = service.run(trade_date=DAY)

    assert second.id == first.id
    assert second.status == "SUCCEEDED"
    assert second.ready_symbols == 2
    assert second.failed_symbols == 0


def test_force_refresh_bypasses_successful_bootstrap_cache(session):
    plan = _plan()
    client = FixtureHistoryClient(rows=20)
    service = _service(session, client, plan)
    service.run(trade_date=DAY)
    first_calls = len(client.calls)
    service.run(trade_date=DAY, force_refresh=True)
    assert len(client.calls) > first_calls


def test_fixture_bootstrap_closes_candidate_discovery_history_gap(session):
    from test_candidate_discovery_cold_start import (
        _seed_analysis_context,
        _service as discovery_service,
    )

    _seed_analysis_context(session)
    plan = _plan(("300001",), minimum_rows=55)
    bootstrap = _service(
        session,
        FixtureHistoryClient(rows=55),
        plan,
    ).run(trade_date=DAY)
    discovery = discovery_service(session).run(now=NOW)

    assert bootstrap.status == "SUCCEEDED"
    assert bootstrap.coverage_ratio == Decimal("1")
    assert discovery.status == "COMPLETED"
    assert discovery.blocked_reasons == []
    assert discovery.candidates_generated == 1
