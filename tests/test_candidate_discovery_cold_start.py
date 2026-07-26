from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.config import Settings
from app.data_hub.contracts import (
    IndustryCapitalFlow,
    IndustryConstituent,
    IndustryDaily,
    ObservedRows,
    ProviderMetadata,
)
from app.data_hub.market_subjects import (
    index_daily_subject,
    industry_universe_subject,
    market_amount_subject,
    market_breadth_subject,
    stock_daily_subject,
    stock_turnover_subject,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import to_market_storage_naive
from app.discovery.service import CandidateDiscoveryService
from app.models import (
    CandidateDiscoveryRun,
    DataQualityRecord,
    DiscoveryCandidate,
    IndustryAnalysisSnapshot,
    MarketDailyBar,
    MarketRegimeSnapshot,
    MarketTurnoverSnapshot,
)


DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
INDUSTRY = "通信设备"


def _sessions_ending(day: date, count: int) -> list[date]:
    sessions = []
    current = day
    while len(sessions) < count:
        if current.weekday() < 5:
            sessions.append(current)
        current -= timedelta(days=1)
    return sorted(sessions)


class DiscoveryFoundationProvider:
    metadata = ProviderMetadata(
        provider_id="candidate-cold-start-fixture",
        supported_capabilities=(
            "market.industry.daily",
            "market.industry.constituents",
            "market.industry.capital_flow",
            "market.limit_up_pool",
            "market.broken_limit_pool",
        ),
        priority=1,
    )

    def __init__(self, symbols=("300001",)):
        self.symbols = symbols

    @property
    def provider_id(self):
        return self.metadata.provider_id

    @property
    def configured(self):
        return True

    def credential_status(self):
        return {"configured": True, "required_credentials": []}

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_industry_universe(self, start, end):
        del start, end
        return [
            IndustryDaily(
                industry=INDUSTRY,
                trade_date=trade_day,
                change_pct=Decimal("0.2"),
                amount=Decimal("50000000000"),
                amount_share=Decimal("0.05"),
                advance_ratio=Decimal("0.75"),
                limit_up_count=2,
                leader_strength=Decimal("8"),
                new_high_ratio=Decimal("0.2"),
                observed_at=NOW,
                source="fixture",
                fetched_at=NOW,
            )
            for trade_day in _sessions_ending(DAY, 20)
        ]

    def get_industry_constituents_universe(self):
        return [
            IndustryConstituent(
                industry=INDUSTRY,
                symbol=symbol,
                name=f"测试{symbol}",
                weight=Decimal(len(self.symbols) - index),
                change_pct=Decimal("0.5"),
                latest_price=Decimal("10.5"),
                high_52w=Decimal("12"),
                is_new_high=False,
                observed_at=NOW,
                source="fixture",
                fetched_at=NOW,
            )
            for index, symbol in enumerate(self.symbols)
        ]

    def get_industry_capital_flow(self, day):
        return [
            IndustryCapitalFlow(
                industry_key=INDUSTRY,
                industry_name=INDUSTRY,
                trade_date=day,
                net_inflow_1d=Decimal("100000000"),
                net_inflow_5d=Decimal("300000000"),
                net_inflow_10d=Decimal("500000000"),
                amount=Decimal("2000000000"),
                amount_unit="CNY",
                source="fixture",
                observed_at=NOW.replace(hour=15, minute=0),
                fetched_at=NOW,
            )
        ]

    def get_limit_up_pool(self, day):
        return ObservedRows(
            [], observed_at=NOW.replace(hour=15, minute=0), fetched_at=NOW
        )

    def get_broken_limit_pool(self, day):
        return ObservedRows(
            [], observed_at=NOW.replace(hour=15, minute=0), fetched_at=NOW
        )


def _quality(session, capability, subject, *, row_count=1):
    stored_now = to_market_storage_naive(NOW.replace(hour=15, minute=0))
    record = DataQualityRecord(
        symbol=subject.subject_id if subject.subject_type == "stock" else None,
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=stored_now,
        fetched_at=stored_now,
        provider_id="candidate-cold-start-context",
        provider_observations=[],
        normalized_digest="b" * 64,
        conflict_fields=[],
        row_count=row_count,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    return record


def _binding(capability, subject, record):
    return {
        "capability": capability,
        "subject": subject.model_dump(mode="json"),
        "quality_record_id": record.id,
        "observed_at": NOW.replace(hour=15, minute=0).isoformat(),
    }


def _seed_analysis_context(session):
    breadth_subject = market_breadth_subject()
    amount_subject = market_amount_subject()
    index_subject = index_daily_subject("CSI000300", "unadjusted", "CNY", "share")
    daily_subject = industry_universe_subject()
    constituent_subject = industry_universe_subject(constituents=True)
    breadth = _quality(session, "market.breadth.daily", breadth_subject)
    amount = _quality(session, "market.amount.daily", amount_subject)
    index = _quality(session, "market.index_daily", index_subject, row_count=55)
    industry_daily = _quality(session, "market.industry.daily", daily_subject, row_count=20)
    industry_members = _quality(
        session,
        "market.industry.constituents",
        constituent_subject,
    )
    market_bindings = [
        _binding("market.breadth.daily", breadth_subject, breadth),
        _binding("market.amount.daily", amount_subject, amount),
        _binding("market.index_daily", index_subject, index),
    ]
    industry_bindings = [
        _binding("market.industry.daily", daily_subject, industry_daily),
        _binding("market.industry.constituents", constituent_subject, industry_members),
        _binding("market.index_daily", index_subject, index),
    ]
    stored_now = to_market_storage_naive(NOW.replace(hour=15, minute=0))
    session.add_all(
        [
            MarketRegimeSnapshot(
                market_id="CN-A",
                trade_date=DAY,
                state="EXPANSION",
                previous_state="REPAIR",
                transition="REPAIR->EXPANSION",
                product_snapshot_hash="c" * 64,
                observed_at=stored_now,
                quality_status="SINGLE_SOURCE",
                quality_bindings=market_bindings,
            ),
            IndustryAnalysisSnapshot(
                industry_name=INDUSTRY,
                trade_date=DAY,
                classification="MAINLINE",
                product_snapshot_hash="d" * 64,
                observed_at=stored_now,
                quality_status="SINGLE_SOURCE",
                quality_bindings=industry_bindings,
            ),
        ]
    )
    session.commit()
    return index


def _seed_market_history(session, index_quality):
    stock_daily = _quality(
        session,
        "market.daily.qfq",
        stock_daily_subject("300001", "qfq", "CNY", "share"),
        row_count=55,
    )
    turnover = _quality(
        session,
        "market.turnover.daily",
        stock_turnover_subject("300001"),
        row_count=55,
    )
    stored_now = to_market_storage_naive(NOW.replace(hour=15, minute=0))
    for offset, trade_day in enumerate(_sessions_ending(DAY, 55)):
        index_close = Decimal("100") + Decimal(offset) * Decimal("0.05")
        stock_close = Decimal("10") + Decimal(offset) * Decimal("0.01")
        session.add_all(
            [
                MarketDailyBar(
                    symbol="CSI000300",
                    trade_date=trade_day,
                    open=index_close,
                    high=index_close,
                    low=index_close,
                    close=index_close,
                    volume=Decimal("1000000"),
                    adjustment="unadjusted",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=stored_now,
                    quality_status="SINGLE_SOURCE",
                    quality_record_id=index_quality.id,
                    source="fixture-index",
                    fetched_at=stored_now,
                ),
                MarketDailyBar(
                    symbol="300001",
                    trade_date=trade_day,
                    open=stock_close,
                    high=stock_close,
                    low=stock_close,
                    close=stock_close,
                    volume=Decimal("1000000"),
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=stored_now,
                    quality_status="SINGLE_SOURCE",
                    quality_record_id=stock_daily.id,
                    source="fixture-stock",
                    fetched_at=stored_now,
                ),
                MarketTurnoverSnapshot(
                    symbol="300001",
                    trade_date=trade_day,
                    turnover_rate=Decimal("3"),
                    amount=Decimal("200000000"),
                    observed_at=stored_now,
                    source="fixture",
                    fetched_at=stored_now,
                    quality_record_id=turnover.id,
                ),
            ]
        )
    session.commit()


def _service(session, *, provider=None):
    registry = ProviderRegistry()
    registry.register(provider or DiscoveryFoundationProvider())
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    settings = Settings(
        candidate_min_history_coverage_ratio=Decimal("0.8"),
        candidate_discovery_max_industries=3,
        candidate_discovery_max_per_industry=10,
        candidate_discovery_max_candidates=30,
    )
    return CandidateDiscoveryService(session, router=router, settings=settings)


def test_empty_history_cold_start_blocks_with_explicit_benchmark_reason(session):
    _seed_analysis_context(session)
    run = _service(session).run(now=NOW)
    assert run.status == "BLOCKED"
    assert run.blocked_reasons == ["BENCHMARK_HISTORY_MISSING"]
    assert run.candidates_generated == 0
    assert session.scalar(select(func.count(DiscoveryCandidate.id))) == 0


def test_prepared_history_real_data_hub_path_generates_deterministic_candidate(session):
    index_quality = _seed_analysis_context(session)
    _seed_market_history(session, index_quality)
    run = _service(session).run(now=NOW)
    candidate = session.scalar(
        select(DiscoveryCandidate).where(DiscoveryCandidate.discovery_run_id == run.id)
    )
    assert run.status == "COMPLETED"
    assert run.total_constituents == 1
    assert run.historical_data_ready == 1
    assert run.historical_data_missing == 0
    assert run.coverage_ratio == Decimal("1")
    assert candidate is not None
    assert candidate.symbol == "300001"
    assert session.scalar(select(func.count(CandidateDiscoveryRun.id))) == 1


def test_partial_history_universe_blocks_without_silently_shrinking(session):
    index_quality = _seed_analysis_context(session)
    _seed_market_history(session, index_quality)
    run = _service(
        session,
        provider=DiscoveryFoundationProvider(symbols=("300001", "300002")),
    ).run(now=NOW)
    assert run.status == "BLOCKED"
    assert run.blocked_reasons == [
        "HISTORICAL_UNIVERSE_COVERAGE_INSUFFICIENT"
    ]
    assert run.total_constituents == 2
    assert run.historical_data_ready == 1
    assert run.historical_data_missing == 1
    assert run.coverage_ratio == Decimal("0.5")
    assert run.candidates_generated == 0
