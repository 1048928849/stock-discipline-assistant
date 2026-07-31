from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.composition.strategies import build_strategy_registry
from app.data_hub.contracts import DailyBar, DataProvider, ProviderMetadata
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import get_trading_calendar
from app.models import Account, DataQualityRecord, Holding, SelectedStockAnalysisRun
from app.selected_stock.contracts import (
    AccountContext,
    ContextStatus,
    DataStatus,
    GateStatus,
    SelectedStockAnalysisRequest,
    StrategyMode,
    UserPriceStatus,
)
from app.selected_stock.indicators import calculate_indicators
from app.selected_stock.replay import replay_selected_stock
from app.selected_stock.service import SelectedStockAnalysisService
from app.selected_stock.strategy import (
    DEFAULT_PARAMETERS,
    CycleStructureValidationStrategyV2,
    classify_cycle_state,
    classify_stock_role,
)
from app.selected_stock.contracts import CycleState, StockRole


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=SHANGHAI)
END = date(2026, 7, 31)


def _sessions(count: int, end: date = END) -> list[date]:
    calendar = get_trading_calendar()
    result = []
    day = end
    while len(result) < count:
        if calendar.is_session(day):
            result.append(day)
        day -= timedelta(days=1)
    return sorted(result)


def _daily_rows(count: int = 280, *, symbol: str = "300308") -> list[DailyBar]:
    calendar = get_trading_calendar()
    rows = []
    for index, day in enumerate(_sessions(count)):
        close = Decimal("10") + Decimal(index) / Decimal("100")
        rows.append(
            DailyBar(
                symbol=symbol,
                trade_date=day,
                open=close - Decimal("0.03"),
                high=close + Decimal("0.08"),
                low=close - Decimal("0.08"),
                close=close,
                volume=Decimal("1000000") + Decimal(index * 1000),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=calendar.session_close_at(day),
                source="fixture-stock-provider",
                fetched_at=NOW,
            )
        )
    return rows


def _indicator_rows(count: int = 280, *, offset: Decimal = Decimal("0")):
    return [
        {
            "trade_date": row.trade_date,
            "open": row.open + offset,
            "high": row.high + offset,
            "low": row.low + offset,
            "close": row.close + offset,
            "volume": row.volume,
        }
        for row in _daily_rows(count)
    ]


class _FixtureProvider(DataProvider):
    metadata = ProviderMetadata(
        provider_id="selected-stock-fixture",
        supported_capabilities=("market.daily.qfq", "market.index_daily"),
        priority=1,
        health_status="READY",
    )

    def health_check(self, probe: bool = False):
        return {"status": "READY"}

    def get_history(self, symbol: str, start: date, end: date):
        return [row for row in _daily_rows(symbol=symbol) if start <= row.trade_date <= end]

    def get_index_history(self, symbol: str, start: date, end: date):
        rows = []
        for index, day in enumerate(_sessions(280)):
            close = Decimal("4000") + Decimal(index)
            rows.append(
                {
                    "date": day,
                    "code": "sh.000300",
                    "open": close - Decimal("2"),
                    "high": close + Decimal("5"),
                    "low": close - Decimal("5"),
                    "close": close,
                    "volume": Decimal("100000000"),
                }
            )
        return {
            "rows": rows,
            "source": "fixture-csi300-provider",
            "fetched_at": NOW,
            "provider_lineage": {
                "request_digest": "a" * 64,
                "response_digest": "b" * 64,
                "adapter_version": "fixture-1",
            },
        }


def test_request_requires_aware_current_price_time():
    with pytest.raises(ValidationError, match="timezone-aware"):
        SelectedStockAnalysisRequest(
            stock_code="300308",
            current_price=Decimal("20"),
            current_price_observed_at=datetime(2026, 7, 30, 15, 0),
        )


def test_indicators_are_deterministic_and_cover_required_groups():
    stock = _indicator_rows()
    benchmark = _indicator_rows(offset=Decimal("100"))
    industry = _indicator_rows(offset=Decimal("20"))
    first = calculate_indicators(
        stock,
        benchmark_rows=benchmark,
        industry_rows=industry,
    )
    second = calculate_indicators(
        stock,
        benchmark_rows=benchmark,
        industry_rows=industry,
    )
    assert first == second
    assert first["sma"]["120"] is not None
    assert first["rsi14"] is not None
    assert first["macd"] is not None
    assert first["atr14"] is not None
    assert first["bollinger"]["width"] is not None
    assert first["obv"] != 0
    assert first["drawdown"]["120"] is not None
    assert len(first["support_zone"]) == 2
    assert len(first["resistance_zone"]) == 2
    assert first["relative_strength"]["stock_vs_csi300_60"] is not None


def test_strategy_manifest_and_all_hard_gates_are_stable():
    strategy = CycleStructureValidationStrategyV2()
    manifest = strategy.manifest()
    assert manifest.strategy_id == "cycle_structure_validation_v2"
    assert manifest.version == "2.0.0"
    assert manifest.default_parameters == DEFAULT_PARAMETERS
    expected = {
        "DATA_INSUFFICIENT",
        "DATA_STALE",
        "DATA_CONFLICTED",
        "NO_CLEAR_INVALIDATION",
        "STOP_TOO_FAR",
        "RISK_REWARD_INSUFFICIENT",
        "DOWNTREND_STRUCTURE",
        "DECLINE_CYCLE",
        "CLIMAX_NEW_ENTRY_BLOCKED",
        "FOLLOWER_WITHOUT_LEADER_CONFIRMATION",
        "INDUSTRY_CONTEXT_REQUIRED_BUT_MISSING",
        "LOSS_POSITION_ADD_BLOCKED",
        "T1_NEW_POSITION_RISK_EXCEEDED",
        "MAX_POSITION_EXCEEDED",
        "DAILY_LOSS_LIMIT_REACHED",
        "SINGLE_STOCK_LOSS_LIMIT",
    }
    indicators = calculate_indicators(
        _indicator_rows(),
        benchmark_rows=_indicator_rows(offset=Decimal("100")),
    )
    result = strategy.build_plan(
        request=SelectedStockAnalysisRequest(stock_code="300308"),
        generated_at=NOW,
        analysis_date=END,
        data_status=DataStatus.FRESH,
        market_status=ContextStatus.BREADTH_UNAVAILABLE,
        industry_status=ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE,
        industry_name=None,
        stock_row_count=280,
        indicators=indicators,
        quality_bindings=(),
        source_lineage=(),
        snapshot_hash="c" * 64,
        product_v1_status="FORMAL_EXECUTION_REMAINS_PRODUCT_V1",
        market_price_observed_at=get_trading_calendar().session_close_at(END),
    )
    assert {item.code for item in result.hard_gates} == expected
    assert result.executable is False
    assert "PENDING_INTRADAY_CONFIRMATION" in result.pending_conditions
    assert result.product_v1_comparison.formal_execution_owner == "PRODUCT_V1"


def test_csv_v2_is_registered_but_not_product_v1_enabled():
    registry = build_strategy_registry()
    assert registry.get("cycle_structure_validation_v2", "2.0.0")
    assert not registry.is_enabled("cycle_structure_validation_v2", "2.0.0")
    assert [(item.strategy_id, item.strategy_version) for item in registry.enabled_strategies()] == [
        ("core.discipline", "1.0.0")
    ]


def test_selected_stock_service_persists_exact_lineage_and_is_idempotent(session):
    providers = ProviderRegistry()
    providers.register(_FixtureProvider())
    router = DataHubRouter(
        session,
        providers,
        calendar=get_trading_calendar(),
        now_fn=lambda: NOW,
    )
    service = SelectedStockAnalysisService(
        session,
        router=router,
        calendar=get_trading_calendar(),
        now_fn=lambda: NOW,
    )
    request = SelectedStockAnalysisRequest(
        stock_code="300308",
        analysis_date=END,
        strategy_mode=StrategyMode.CSV_V2_ADVISORY,
        account_size=Decimal("300000"),
    )

    first = service.analyze(request)
    second = service.analyze(request)

    assert first.analysis_run_id == second.analysis_run_id
    assert first.snapshot_hash == second.snapshot_hash
    assert first.result_digest == second.result_digest
    assert first.market_context_status == ContextStatus.BREADTH_UNAVAILABLE
    assert first.industry_context_status == ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE
    assert first.executable is False
    assert first.product_v1_comparison.product_v1_status.startswith("BLOCKED")
    assert first.product_v1_comparison.conflict_status == "DATA_CONFLICT"
    assert first.technical_evidence["product_v1_shadow_signal_hash"]
    assert len(first.quality_bindings) == 2
    assert len(first.source_lineage) == 4
    assert {item.capability for item in first.source_lineage} >= {
        "selected_stock.price_observation",
        "selected_stock.account_context",
    }
    assert first.source_lineage[0].details["persisted_row_count"] == 280
    assert first.source_lineage[0].details["analysis_row_count"] == 280
    assert len(session.scalars(select(SelectedStockAnalysisRun)).all()) == 1
    records = session.scalars(select(DataQualityRecord)).all()
    assert records
    assert all(record.persisted for record in records if record.quality_status != "MISSING")


def test_selected_stock_service_loads_server_account_and_rejects_manual_conflict(session):
    account = Account(
        name="selected-stock-server-account",
        total_assets=Decimal("100000"),
        cash=Decimal("70000"),
        available_cash=Decimal("60000"),
    )
    session.add(account)
    session.flush()
    session.add(
        Holding(
            account_id=account.id,
            symbol="300308",
            name="fixture",
            quantity=100,
            cost_price=Decimal("10"),
            current_price=Decimal("12"),
            price_source="fixture",
        )
    )
    session.commit()
    providers = ProviderRegistry()
    providers.register(_FixtureProvider())
    service = SelectedStockAnalysisService(
        session,
        router=DataHubRouter(
            session,
            providers,
            calendar=get_trading_calendar(),
            now_fn=lambda: NOW,
        ),
        calendar=get_trading_calendar(),
        now_fn=lambda: NOW,
    )

    loaded = service.analyze(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            analysis_date=END,
            account_id=account.id,
        )
    )
    assert loaded.account_context.source == "SERVER_ACCOUNT"
    assert loaded.account_context.trust_status == "SERVER_LOADED"
    assert loaded.account_context.current_position_quantity == 100
    assert loaded.account_context.available_cash == Decimal("60000.0000")

    conflict = service.analyze(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            analysis_date=END,
            account_id=account.id,
            account_size=Decimal("200000"),
        )
    )
    assert conflict.account_context.trust_status == "ACCOUNT_INPUT_CONFLICT"
    assert "account_size" in conflict.account_context.conflict_fields
    assert "ACCOUNT_INPUT_CONFLICT" in conflict.execution_blockers
    assert conflict.position_plan.quantity is None


def test_analysis_result_is_immutable(session):
    column = SelectedStockAnalysisRun.__table__.c.result_snapshot
    assert column.nullable is False
    assert SelectedStockAnalysisRun.__table__.c.analysis_identity_hash.unique is None


def test_historical_replay_never_uses_future_rows_to_build_plan():
    rows = _indicator_rows(280)
    analysis_date = rows[-21]["trade_date"]
    request = SelectedStockAnalysisRequest(
        stock_code="300308",
        analysis_date=analysis_date,
    )
    first = replay_selected_stock(
        request=request,
        generated_at=NOW,
        stock_rows=rows,
        benchmark_rows=_indicator_rows(280, offset=Decimal("100")),
    )
    changed_future = [dict(row) for row in rows]
    for row in changed_future:
        if row["trade_date"] > analysis_date:
            row["open"] = row["high"] = row["low"] = row["close"] = Decimal("999")
    second = replay_selected_stock(
        request=request,
        generated_at=NOW,
        stock_rows=changed_future,
        benchmark_rows=_indicator_rows(280, offset=Decimal("100")),
    )
    assert first.plan.result_digest == second.plan.result_digest
    assert first.horizons != second.horizons
    assert [item.sessions for item in first.horizons] == [5, 10, 20]


@pytest.mark.parametrize(
    ("overrides", "market", "industry", "expected"),
    [
        ({"pullback_on_low_volume": True}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.PREPARATION),
        ({"ma_entanglement": True}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.PROBE),
        ({"bullish_alignment": True, "breakout_on_volume": True}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.START_CONFIRMED),
        ({"bullish_alignment": True, "relative_strength": {"stock_vs_csi300_20": Decimal("0.1"), "industry_vs_csi300_20": Decimal("0.02")}}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.MARKUP),
        ({"bullish_alignment": True, "relative_strength": {"stock_vs_csi300_20": Decimal("-0.1")}}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.DIVERGENCE),
        ({"market_context": {"state": "DIVERGENCE"}, "relative_strength": {"stock_vs_industry_20": Decimal("0.1")}}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.CONCENTRATION),
        ({"rsi14": Decimal("80"), "atr_pct": Decimal("0.05")}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.CLIMAX),
        ({"bearish_alignment": True}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.DECLINE),
        ({}, ContextStatus.AVAILABLE, ContextStatus.AVAILABLE, CycleState.TRANSITION),
        ({}, ContextStatus.BREADTH_UNAVAILABLE, ContextStatus.AVAILABLE, CycleState.UNKNOWN),
    ],
)
def test_all_cycle_states_have_deterministic_evidence_paths(overrides, market, industry, expected):
    base = {
        "bearish_alignment": False,
        "bullish_alignment": False,
        "large_bearish_streak": False,
        "rsi14": Decimal("50"),
        "atr_pct": Decimal("0.01"),
        "breakout_on_volume": False,
        "pullback_on_low_volume": False,
        "ma_entanglement": False,
        "relative_strength": {},
    }
    base.update(overrides)
    assert classify_cycle_state(base, market_status=market, industry_status=industry) == expected


@pytest.mark.parametrize(
    ("evidence", "relative", "expected"),
    [
        ({"constituent_rank": 1}, Decimal("0.12"), StockRole.LEADER),
        ({"amount_rank_percentile": Decimal("0.05"), "industry_weight": Decimal("0.08")}, Decimal("0.01"), StockRole.CAPACITY_CORE),
        ({"branch_rank": 1}, Decimal("0.01"), StockRole.BRANCH_CORE),
        ({}, Decimal("0.08"), StockRole.TREND_CORE),
        ({"early_rotation": True}, Decimal("0.01"), StockRole.ROTATION_FRONT),
        ({}, Decimal("-0.08"), StockRole.FOLLOWER),
        ({"event_driven": True}, Decimal("0"), StockRole.EVENT_DRIVEN),
        ({}, Decimal("0"), StockRole.UNKNOWN),
    ],
)
def test_all_stock_roles_require_structured_evidence(evidence, relative, expected):
    indicators = {
        "role_evidence": evidence,
        "relative_strength": {"stock_vs_industry_20": relative},
    }
    assert classify_stock_role(indicators, industry_status=ContextStatus.AVAILABLE) == expected
    assert classify_stock_role(indicators, industry_status=ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE) == StockRole.UNKNOWN


def _risk_result(
    request: SelectedStockAnalysisRequest,
    *,
    account_context: AccountContext | None = None,
    parameters: dict | None = None,
):
    indicators = calculate_indicators(
        _indicator_rows(),
        benchmark_rows=_indicator_rows(offset=Decimal("100")),
    )
    return CycleStructureValidationStrategyV2().build_plan(
        request=request,
        generated_at=NOW,
        analysis_date=END,
        data_status=DataStatus.FRESH,
        market_status=ContextStatus.AVAILABLE,
        industry_status=ContextStatus.AVAILABLE,
        industry_name="fixture-industry",
        stock_row_count=280,
        indicators=indicators,
        quality_bindings=(),
        source_lineage=(),
        snapshot_hash="e" * 64,
        product_v1_status="FORMAL_EXECUTION_REMAINS_PRODUCT_V1",
        market_price_observed_at=get_trading_calendar().session_close_at(END),
        account_context=account_context,
        parameters=parameters,
    )


def _gate_for(result, code: str):
    return next(item for item in result.hard_gates if item.code == code)


def test_missing_daily_loss_is_not_evaluated_and_cannot_add_score_or_quantity():
    result = _risk_result(
        SelectedStockAnalysisRequest(stock_code="300308", account_size=Decimal("100000"))
    )
    gate = _gate_for(result, "DAILY_LOSS_LIMIT_REACHED")
    assert gate.status == GateStatus.NOT_EVALUATED
    assert gate.reason_code == "DAILY_LOSS_CONTEXT_UNAVAILABLE"
    assert result.scores.risk_invalidation.score == 0
    assert result.position_plan.quantity is None


def test_daily_loss_limit_is_a_real_blocking_calculation():
    result = _risk_result(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            account_size=Decimal("100000"),
            daily_realized_pnl=Decimal("-2500"),
            daily_unrealized_pnl=Decimal("0"),
            daily_pnl_observed_at=NOW,
            daily_pnl_source="USER_ACCOUNT_OBSERVATION",
        )
    )
    gate = _gate_for(result, "DAILY_LOSS_LIMIT_REACHED")
    assert gate.status == GateStatus.BLOCKED
    assert result.account_context.daily_loss_pct == Decimal("2.500000")
    assert result.position_plan.quantity is None


def test_t1_and_new_position_allocation_limits_are_calculated():
    result = _risk_result(
        SelectedStockAnalysisRequest(stock_code="300308", account_size=Decimal("100000")),
        parameters={"max_new_position_pct": 5},
    )
    gate = _gate_for(result, "T1_NEW_POSITION_RISK_EXCEEDED")
    assert gate.status == GateStatus.BLOCKED
    assert "proposed_pct=10.0000" in gate.evidence


def test_post_trade_max_position_and_losing_add_are_blocked():
    result = _risk_result(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            account_size=Decimal("100000"),
            current_position_quantity=100,
            current_position_pct=Decimal("25"),
            average_cost=Decimal("100"),
        )
    )
    assert _gate_for(result, "MAX_POSITION_EXCEEDED").status == GateStatus.BLOCKED
    assert _gate_for(result, "LOSS_POSITION_ADD_BLOCKED").status == GateStatus.BLOCKED
    assert result.holding_plan is not None
    assert result.holding_plan.allow_add is False


def test_single_stock_loss_limit_is_bound_to_account_equity():
    result = _risk_result(
        SelectedStockAnalysisRequest(stock_code="300308", account_size=Decimal("100000")),
        parameters={"single_stock_loss_limit_pct": 0},
    )
    assert _gate_for(result, "SINGLE_STOCK_LOSS_LIMIT").status == GateStatus.BLOCKED


@pytest.mark.parametrize(
    ("observed_at", "expected"),
    [
        (NOW + timedelta(seconds=1), UserPriceStatus.USER_PRICE_FUTURE),
        (NOW - timedelta(hours=1), UserPriceStatus.USER_PRICE_STALE),
        (
            datetime(2026, 7, 30, 14, 0, tzinfo=SHANGHAI),
            UserPriceStatus.USER_PRICE_DATE_MISMATCH,
        ),
    ],
)
def test_untrusted_user_price_never_generates_quantity(observed_at, expected):
    result = _risk_result(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            current_price=Decimal("12.79"),
            current_price_observed_at=observed_at,
            account_size=Decimal("100000"),
        )
    )
    assert result.price_observation.trust_status == expected
    assert result.price_observation.executable_for_position is False
    assert result.position_plan.quantity is None


def test_position_quantity_and_percentage_conflict_blocks_add_calculation():
    result = _risk_result(
        SelectedStockAnalysisRequest(
            stock_code="300308",
            account_size=Decimal("100000"),
            current_position_quantity=100,
            current_position_pct=Decimal("50"),
            average_cost=Decimal("10"),
        )
    )
    assert "POSITION_INPUT_CONFLICT" in result.execution_blockers
    assert result.position_plan.quantity is None
    assert result.holding_plan is not None
    assert result.holding_plan.allow_add is False
