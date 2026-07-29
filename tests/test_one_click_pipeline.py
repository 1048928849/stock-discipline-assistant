from datetime import date, datetime, timedelta, timezone
import inspect as python_inspect
from decimal import Decimal
from threading import Barrier, Lock, Thread

import pytest
from sqlalchemy import create_engine, insert, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.data_hub.contracts import (
    DailyBar,
    DataProvider,
    IndustryConstituent,
    IndustryDaily,
    IntradayBar,
    MarketAmountDaily,
    MarketBreadthDaily,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
    TurnoverDaily,
)
from app.data_hub.market_subjects import stock_daily_subject
from app.data_hub.research_subjects import announcement_catalog_window
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    TradingPhase,
    XSHGTradingCalendar,
    get_trading_calendar,
    shanghai_now,
    shanghai_today,
    to_shanghai_aware,
)
from app.database import Base
from app.domain.models import DecisionPackage, MARKET_EVIDENCE_CAPABILITIES
from app.errors import AppError
from app.models import (
    CompanyAnnouncement,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchRefresh,
    CompanyValuationSnapshot,
    DataQualityRecord,
    MarketDailyBar,
    MarketQuote,
    PlanAnalysisRun,
    TradePlan,
)
from app.schemas_workflow import OneClickPlanRequest, TradePlanPreviewRequest
from app.services.one_click_pipeline import (
    _ensure_profile,
    _market_assessment,
    confirm_one_click_plan,
    run_one_click_analysis,
)
from app.services.market_cache import (
    mapping_series_bars,
    persist_market_quote,
    replace_market_series,
)
from app.services.research_cache import (
    persist_announcement_catalog,
    persist_company_profile,
)
from app.services.trade_plan_generator import generate_trade_plan_preview


def create_account(client, assets="100000", cash="80000"):
    return client.post(
        "/api/v1/accounts",
        json={
            "name": f"账户{assets}",
            "total_assets": assets,
            "cash": cash,
            "available_cash": cash,
        },
    ).json()


def seed_pattern(session, symbol="300502"):
    now = shanghai_now()
    rows = []
    for index in range(260):
        close = 5 + index * 0.019
        rows.append((close, close + 0.08, close - 0.08, 100.0))
    for index in range(25):
        close = 10 + (index % 3 - 1) * 0.03
        rows.append((close, close + 0.12, close - 0.12, 100.0))
    rows.extend([(10.55, 10.65, 10.15, 220.0), (10.32, 10.48, 10.12, 55.0)])
    rows.append((10.82, 10.9, 10.3, 180.0))
    calendar = get_trading_calendar()
    latest = calendar.latest_completed_session(now)
    trade_dates = []
    candidate = latest
    while len(trade_dates) < len(rows):
        try:
            calendar.session_close_at(candidate)
        except ValueError:
            candidate -= timedelta(days=1)
            continue
        trade_dates.append(candidate)
        candidate -= timedelta(days=1)
    trade_dates.reverse()
    bars = [
        DailyBar(
            symbol=symbol,
            trade_date=trade_dates[index],
            open=Decimal(str(close - 0.03)),
            high=Decimal(str(high)),
            low=Decimal(str(low)),
            close=Decimal(str(close)),
            volume=Decimal(str(volume)),
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            observed_at=calendar.session_close_at(trade_dates[index]),
            source="akshare_tencent_qfq",
            fetched_at=now,
        )
        for index, (close, high, low, volume) in enumerate(rows)
    ]

    class PatternProvider(DataProvider):
        metadata = ProviderMetadata(
            provider_id="pattern-fixture",
            supported_capabilities=(
                "market.daily.qfq",
                "market.quote.realtime",
            ),
            priority=1,
            realtime_supported=True,
        )

        def health_check(self, probe: bool = False):
            return {"status": "healthy"}

        def get_history(self, requested_symbol, date_from, date_to):
            return bars

        def get_quote(self, requested_symbol):
            return Quote(
                symbol=requested_symbol,
                name="测试公司",
                price=Decimal("10.82"),
                quote_type="realtime",
                observed_at=now,
                price_unit="CNY",
                source="akshare_sina",
                source_api="stock_zh_a_spot",
                fetched_at=now,
            )

    registry = ProviderRegistry()
    registry.register(PatternProvider())
    router = DataHubRouter(session, registry)
    history = router.get_history(symbol, trade_dates[0], latest)
    replace_market_series(
        session,
        router,
        history,
        history.require_value(),
        subject=stock_daily_subject(symbol, "qfq", "CNY", "share"),
        min_rows=250,
    )
    quote = router.get_quote(symbol)
    persist_market_quote(session, router, quote)
    session.commit()


def benchmark_rows(direction="up"):
    now = shanghai_now()
    calendar = get_trading_calendar()
    latest = calendar.latest_completed_session(now)
    trade_dates = []
    candidate = latest
    while len(trade_dates) < 80:
        try:
            calendar.session_close_at(candidate)
        except ValueError:
            candidate -= timedelta(days=1)
            continue
        trade_dates.append(candidate)
        candidate -= timedelta(days=1)
    trade_dates.reverse()
    rows = []
    for index, trade_date in enumerate(trade_dates):
        close = 100 + index if direction == "up" else 200 - index * 1.2
        rows.append({"date": trade_date, "close": close, "volume": 1000})
    return {"rows": rows, "source": "测试指数", "fetched_at": now}


class QuoteScenarioProvider(DataProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        price: str = "10.82",
        quote_type: str = "realtime",
        priority: int = 1,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.quote.realtime",
                "market.quote.latest_close",
            ),
            priority=priority,
            realtime_supported=True,
        )
        self.price = Decimal(price)
        self.quote_type = quote_type

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_quote(self, symbol):
        now = shanghai_now()
        if self.quote_type == "latest_close":
            calendar = get_trading_calendar()
            now = calendar.session_close_at(calendar.latest_completed_session())
        return Quote(
            symbol=symbol,
            name="测试公司",
            price=self.price,
            quote_type=self.quote_type,
            observed_at=now,
            price_unit="CNY",
            source=self.provider_id,
            source_api="quote-fixture",
            fetched_at=now,
        )


class BindingScenarioProvider(DataProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        close: str = "10",
        quote_price: str = "10.82",
        priority: int = 1,
        fail: bool = False,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.daily.qfq",
                "market.quote.realtime",
                "market.index_daily",
                "market.sector_daily",
            ),
            priority=priority,
            realtime_supported=True,
        )
        self.close = Decimal(close)
        self.quote_price = Decimal(quote_price)
        self.fail = fail

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def _check(self):
        if self.fail:
            raise ProviderUnavailableError("binding test refresh failed")

    def get_quote(self, symbol):
        self._check()
        now = shanghai_now()
        return Quote(
            symbol=symbol,
            name="binding-test",
            price=self.quote_price,
            quote_type="realtime",
            observed_at=now,
            price_unit="CNY",
            source=self.metadata.provider_id,
            source_api="binding-test",
            fetched_at=now,
        )

    def get_history(self, symbol, start, end):
        self._check()
        now = shanghai_now()
        calendar = get_trading_calendar()
        latest = min(end, calendar.latest_completed_session(now))
        trade_dates = []
        candidate = latest
        while len(trade_dates) < 260:
            try:
                calendar.session_close_at(candidate)
            except ValueError:
                candidate -= timedelta(days=1)
                continue
            trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        return [
            DailyBar(
                symbol=symbol,
                trade_date=trade_date,
                open=self.close,
                high=self.close + Decimal("0.1"),
                low=self.close - Decimal("0.1"),
                close=self.close,
                volume=Decimal("1000"),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=calendar.session_close_at(trade_date),
                source=self.metadata.provider_id,
                fetched_at=now,
            )
            for trade_date in trade_dates
        ]

    def _series(self, end):
        now = shanghai_now()
        calendar = get_trading_calendar()
        latest = min(end, calendar.latest_completed_session(now))
        trade_dates = []
        candidate = latest
        while len(trade_dates) < 80:
            try:
                calendar.session_close_at(candidate)
            except ValueError:
                candidate -= timedelta(days=1)
                continue
            trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        return {
            "rows": [
                {
                    "date": trade_date,
                    "close": self.close,
                    "volume": Decimal("1000"),
                }
                for trade_date in trade_dates
            ],
            "source": self.metadata.provider_id,
            "fetched_at": now,
        }

    def get_index_history(self, symbol, start, end):
        self._check()
        return self._series(end)

    def get_sector_history(self, industry, start, end):
        self._check()
        return self._series(end)


class FixedBenchmarkProvider(DataProvider):
    def __init__(self, *, evaluated_at, include_future_row=False):
        self.metadata = ProviderMetadata(
            provider_id="fixed-benchmark",
            supported_capabilities=("market.index_daily",),
            priority=1,
        )
        self.evaluated_at = evaluated_at
        self.include_future_row = include_future_row
        self.calls = []

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def get_index_history(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        calendar = XSHGTradingCalendar()
        latest = end + timedelta(days=1) if self.include_future_row else end
        trade_dates = []
        candidate = latest
        while len(trade_dates) < 80:
            try:
                calendar.session_close_at(candidate)
            except ValueError:
                candidate -= timedelta(days=1)
                continue
            trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        return {
            "rows": [
                {"date": day, "close": Decimal("100"), "volume": Decimal("1")}
                for day in trade_dates
            ],
            "source": "fixed-benchmark",
            "fetched_at": self.evaluated_at,
        }


def _fixed_benchmark_router(session, provider, evaluated_at):
    registry = ProviderRegistry()
    registry.register(provider)
    return DataHubRouter(session, registry, now_fn=lambda: evaluated_at)


def test_market_assessment_uses_completed_session_and_canonical_csi300(session):
    evaluated_at = datetime(2026, 7, 29, 9, 41, 15, tzinfo=SHANGHAI_TZ)
    provider = FixedBenchmarkProvider(evaluated_at=evaluated_at)
    router = _fixed_benchmark_router(session, provider, evaluated_at)

    _, step = _market_assessment(session, router, evaluated_at=evaluated_at)

    assert provider.calls == [
        ("CSI000300", date(2025, 11, 30), date(2026, 7, 28))
    ]
    assert step["effective_quality"] == "SINGLE_SOURCE"
    assert step["executable"] is True
    record = session.get(DataQualityRecord, step["quality_record_id"])
    assert record.subject_id == "CSI000300"
    assert record.observed_at == datetime(2026, 7, 28, 15, 0)
    assert record.persisted is True
    assert session.query(MarketDailyBar).filter_by(symbol="CSI000300").count() == 80


def test_market_assessment_rejects_rows_after_completed_session(session):
    evaluated_at = datetime(2026, 7, 29, 9, 41, 15, tzinfo=SHANGHAI_TZ)
    provider = FixedBenchmarkProvider(
        evaluated_at=evaluated_at,
        include_future_row=True,
    )
    router = _fixed_benchmark_router(session, provider, evaluated_at)

    _, step = _market_assessment(session, router, evaluated_at=evaluated_at)

    assert step["effective_quality"] == "MISSING"
    assert step["executable"] is False
    assert session.query(MarketDailyBar).filter_by(symbol="CSI000300").count() == 0
    record = session.query(DataQualityRecord).filter_by(
        capability="market.index_daily"
    ).one()
    assert record.persisted is False


def test_market_assessment_reuses_fixed_evaluated_at_for_cache_quality(
    session, monkeypatch
):
    evaluated_at = datetime(2026, 7, 29, 9, 41, 15, tzinfo=SHANGHAI_TZ)
    provider = FixedBenchmarkProvider(evaluated_at=evaluated_at)
    router = _fixed_benchmark_router(session, provider, evaluated_at)
    _, first = _market_assessment(session, router, evaluated_at=evaluated_at)
    monkeypatch.setattr(
        "app.data_hub.effective_quality.shanghai_now",
        lambda: datetime(2030, 1, 1, 10, 0, tzinfo=SHANGHAI_TZ),
    )

    _, second = _market_assessment(session, router, evaluated_at=evaluated_at)

    assert provider.calls == [
        ("CSI000300", date(2025, 11, 30), date(2026, 7, 28))
    ]
    assert first["effective_quality"] == second["effective_quality"]
    assert first["quality_record_id"] == second["quality_record_id"]


class PatternRefreshProvider(BindingScenarioProvider):
    def __init__(self, provider_id, rows, **kwargs):
        self.quote_failure = kwargs.pop("quote_failure", False)
        super().__init__(provider_id, **kwargs)
        self.rows = rows

    def get_quote(self, symbol):
        if self.quote_failure:
            raise ProviderUnavailableError("realtime quote refresh failed")
        return super().get_quote(symbol)

    def get_history(self, symbol, start, end):
        fetched_at = shanghai_now()
        return [
            DailyBar(
                symbol=symbol,
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                adjustment=row.adjustment,
                price_unit=row.price_unit,
                volume_unit=row.volume_unit,
                observed_at=to_shanghai_aware(
                    row.observed_at,
                    naive_is_shanghai=True,
                ),
                source=self.metadata.provider_id,
                fetched_at=fetched_at,
            )
            for row in self.rows
        ]


class SeedResearchProvider(DataProvider):
    metadata = ProviderMetadata(
        provider_id="research-seed",
        supported_capabilities=("fundamental.profile", "announcement.catalog"),
        priority=1,
    )

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def company_profile(self, symbol):
        return {
            "name": "测试公司",
            "industry": "测试行业",
            "market": "创业板",
            "main_business": "测试业务",
        }

    def company_announcements(self, symbol, start, end):
        return [
            {
                "公告标题": "测试公司最新公告",
                "公告日期": end,
                "公告链接": "https://example.test/announcement",
                "目录来源": "exchange_test",
            }
        ]


class ResearchBindingScenarioProvider(SeedResearchProvider):
    def __init__(
        self,
        provider_id,
        *,
        profile_name="测试公司",
        announcements=None,
        fail=False,
        priority=1,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=("fundamental.profile", "announcement.catalog"),
            priority=priority,
        )
        self.profile_name = profile_name
        self.announcements = announcements
        self.fail = fail

    def company_profile(self, symbol):
        if self.fail:
            raise ProviderUnavailableError("profile unavailable")
        value = super().company_profile(symbol)
        return {**value, "name": self.profile_name}

    def company_announcements(self, symbol, start, end):
        if self.fail:
            raise ProviderUnavailableError("catalog unavailable")
        if self.announcements is not None:
            return self.announcements
        return super().company_announcements(symbol, start, end)


class OneClickMutatingProfileRouter(DataHubRouter):
    def __init__(self, session, registry, mutation):
        super().__init__(session, registry)
        self.mutation = mutation
        self.profile_result = None

    def company_profile(self, symbol):
        result = super().company_profile(symbol)
        self.profile_result = result
        self.mutation(self, result)
        return result


def one_click_profile_router(session, mutation=lambda _router, _result: None):
    registry = ProviderRegistry()
    registry.register(SeedResearchProvider())
    return OneClickMutatingProfileRouter(session, registry, mutation)


def seed_profile(session, *, evaluated_at=None):
    registry = ProviderRegistry()
    registry.register(SeedResearchProvider())
    router = DataHubRouter(session, registry)
    profile_result = router.company_profile("300502")
    persist_company_profile(session, router, profile_result)
    start, end = announcement_catalog_window(evaluated_at=evaluated_at)
    announcement_result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(
        session,
        router,
        announcement_result,
        start=start,
        end=end,
    )
    session.commit()


def seed_optional_research_cache(session, acquired_at):
    stored_at = acquired_at.astimezone(timezone.utc).replace(tzinfo=None)
    session.add(
        CompanyFinancialPeriod(
            symbol="300502",
            report_date=acquired_at.date(),
            period_label="test",
            source="test",
            fetched_at=stored_at,
        )
    )
    session.add(
        CompanyValuationSnapshot(
            symbol="300502",
            trade_date=acquired_at.date(),
            source="test",
            fetched_at=stored_at,
        )
    )
    session.commit()


def seed_profile_with_research_times(
    session,
    profile_at,
    announcement_at,
    *,
    evaluated_at=None,
):
    registry = ProviderRegistry()
    registry.register(SeedResearchProvider())
    profile_router = DataHubRouter(session, registry, now_fn=lambda: profile_at)
    profile_result = profile_router.company_profile("300502")
    persist_company_profile(session, profile_router, profile_result)

    announcement_router = DataHubRouter(
        session,
        registry,
        now_fn=lambda: announcement_at,
    )
    start, end = announcement_catalog_window(
        evaluated_at=evaluated_at or announcement_at
    )
    announcement_result = announcement_router.company_announcements(
        "300502",
        start,
        end,
    )
    persist_announcement_catalog(
        session,
        announcement_router,
        announcement_result,
        start=start,
        end=end,
    )
    session.commit()


def _age_profile_for_refresh(session):
    profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
    record = session.get(DataQualityRecord, profile.quality_record_id)
    old_time = datetime.now() - timedelta(days=45)
    profile.fetched_at = old_time
    record.observed_at = old_time
    record.fetched_at = old_time
    record.cached_at = old_time
    session.commit()
    return {
        "name": profile.name,
        "industry": profile.industry,
        "source": profile.source,
        "raw_data": profile.raw_data,
        "fetched_at": profile.fetched_at,
        "quality_record_id": profile.quality_record_id,
    }


def _stored_profile_state(session):
    session.expire_all()
    profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
    return profile, {
        "name": profile.name,
        "industry": profile.industry,
        "source": profile.source,
        "raw_data": profile.raw_data,
        "fetched_at": profile.fetched_at,
        "quality_record_id": profile.quality_record_id,
    }


def test_one_click_profile_refresh_has_no_prevalidated_profile_write(
    session, monkeypatch
):
    router = one_click_profile_router(session)
    original_persist = persist_company_profile

    def assert_clean_entry(db, current_router, result):
        assert not any(
            isinstance(item, CompanyProfile) and item.symbol == "300502"
            for item in db.new | db.dirty
        )
        return original_persist(db, current_router, result)

    monkeypatch.setattr(
        "app.services.one_click_pipeline.persist_company_profile", assert_clean_entry
    )
    profile, step = _ensure_profile(session, "300502", router)
    record = session.get(DataQualityRecord, router.profile_result.quality_record_id)
    assert step["status"] == "success"
    assert profile.quality_record_id == record.id
    assert profile.raw_data == router.profile_result.value
    assert record.persisted is True


def test_one_click_tampered_profile_preserves_existing_profile(session):
    seed_profile(session)
    old_state = _age_profile_for_refresh(session)

    def tamper(_router, result):
        result.value["name"] = "tampered"

    router = one_click_profile_router(session, tamper)
    profile, step = _ensure_profile(session, "300502", router)
    _, stored_state = _stored_profile_state(session)
    assert step["status"] == "partial"
    assert profile is None
    assert stored_state == old_state
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False


def test_one_click_successful_profile_refresh_binds_exact_lineage(session):
    router = one_click_profile_router(session)
    profile, step = _ensure_profile(session, "300502", router)
    binding = step["source_quality_binding"]
    record = session.get(DataQualityRecord, profile.quality_record_id)
    assert binding["quality_record_id"] == profile.quality_record_id
    assert record.persisted is True


def test_one_click_profile_mark_persisted_failure_preserves_cache(
    session, monkeypatch
):
    seed_profile(session)
    old_state = _age_profile_for_refresh(session)
    router = one_click_profile_router(session)

    def fail_mark_persisted(_result, cached_at=None):
        raise ValueError("simulated profile mark_persisted failure")

    monkeypatch.setattr(router, "mark_persisted", fail_mark_persisted)
    profile, step = _ensure_profile(session, "300502", router)
    _, stored_state = _stored_profile_state(session)
    assert step["status"] == "partial"
    assert profile is None
    assert stored_state == old_state
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False


def patch_benchmarks(monkeypatch, market="up", sector="up"):
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_index_history",
        lambda self, symbol, start, end: benchmark_rows(market),
    )
    monkeypatch.setattr(
        "app.providers.akshare_provider.AKShareProvider.get_sector_history",
        lambda self, industry, start, end: benchmark_rows(sector),
    )

    def product_times(self, evaluated_at=None):
        fetched_at = to_shanghai_aware(evaluated_at or shanghai_now())
        business_date = self.calendar.latest_completed_session(fetched_at)
        observed_at = self.calendar.session_close_at(business_date)
        return business_date, observed_at, fetched_at

    def intraday(self, symbol, start, end):
        del start
        business_date, observed_at, fetched_at = product_times(self, end)
        rows = []
        for index in range(21):
            close = Decimal("10.00") + Decimal(index) * Decimal("0.02")
            volume = Decimal("100")
            if index == 20:
                close = Decimal("10.82")
                volume = Decimal("180")
            bar_end = observed_at - timedelta(hours=20 - index)
            rows.append(IntradayBar(
                symbol=symbol,
                trade_date=business_date,
                bar_start=bar_end - timedelta(hours=1),
                bar_end=bar_end,
                open=close - Decimal("0.01"),
                high=close + Decimal("0.03"),
                low=close - Decimal("0.03"),
                close=close,
                volume=volume,
                amount=Decimal("1947.60"),
                turnover_rate=Decimal("2.5"),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=observed_at,
                source="product-test",
                fetched_at=fetched_at,
                completed=True,
            ))
        return rows

    def turnover(self, symbol, start, end):
        del start
        _, observed_at, fetched_at = product_times(self)
        return [
            TurnoverDaily(
                symbol=symbol,
                trade_date=end,
                turnover_rate=Decimal("2.5"),
                amount=Decimal("1947.60"),
                observed_at=observed_at,
                source="product-test",
                fetched_at=fetched_at,
            )
        ]

    def breadth(self, day):
        _, observed_at, fetched_at = product_times(self)
        return [
            MarketBreadthDaily(
                trade_date=day,
                advancing=3200,
                declining=1400,
                unchanged=100,
                limit_up=80,
                limit_down=5,
                new_highs=120,
                new_lows=20,
                median_change_pct=Decimal("0.8"),
                above_ma20_ratio=Decimal("0.65"),
                above_ma50_ratio=Decimal("0.55"),
                observed_at=observed_at,
                source="product-test",
                fetched_at=fetched_at,
            )
        ]

    def amounts(self, start, end):
        del start
        _, observed_at, fetched_at = product_times(self)
        return [
            MarketAmountDaily(
                trade_date=end,
                total_amount=Decimal("1200000000000"),
                observed_at=observed_at,
                source="product-test",
                fetched_at=fetched_at,
            )
        ]

    def industry_universe(self, start, end):
        del start
        _, _, fetched_at = product_times(self)
        trade_dates = []
        candidate = end
        while len(trade_dates) < 20:
            if self.calendar.is_session(candidate):
                trade_dates.append(candidate)
            candidate -= timedelta(days=1)
        trade_dates.reverse()
        return [
            IndustryDaily(
                industry="测试行业",
                trade_date=trade_date,
                change_pct=Decimal("2.2"),
                amount=Decimal("50000000000"),
                amount_share=Decimal("0.05"),
                advance_ratio=Decimal("0.75"),
                limit_up_count=4,
                leader_strength=Decimal("9.9"),
                new_high_ratio=Decimal("0.2"),
                observed_at=self.calendar.session_close_at(trade_date),
                source="product-test",
                fetched_at=fetched_at,
            )
            for trade_date in trade_dates
        ]

    def constituent_universe(self):
        _, observed_at, fetched_at = product_times(self)
        return [
            IndustryConstituent(
                industry="测试行业",
                symbol="300502",
                name="测试公司",
                weight=Decimal("1.5"),
                observed_at=observed_at,
                source="product-test",
                fetched_at=fetched_at,
                change_pct=Decimal("2.2"),
                latest_price=Decimal("10.82"),
                high_52w=Decimal("10.90"),
                is_new_high=False,
            )
        ]

    def concepts(self, symbol):
        del symbol
        _, observed_at, fetched_at = product_times(self)
        return [
            {
                "concept": "测试行业",
                "relevance": "IMPORTANT_BUSINESS",
                "evidence_summary": "deterministic test profile evidence",
                "source": "product-test",
                "observed_at": observed_at,
                "fetched_at": fetched_at,
            }
        ]

    def chains(self, symbol):
        del symbol
        _, observed_at, fetched_at = product_times(self)
        return [
            {
                "chain_name": "测试产业链",
                "node_name": "测试节点",
                "stage": "CORE",
                "relevance": "IMPORTANT_BUSINESS",
                "source": "product-test",
                "evidence_summary": "deterministic test profile evidence",
                "observed_at": observed_at,
                "fetched_at": fetched_at,
            }
        ]

    targets = {
        "get_intraday_60m": intraday,
        "get_turnover_daily": turnover,
        "get_market_breadth": breadth,
        "get_market_amount_history": amounts,
        "get_industry_universe": industry_universe,
        "get_industry_constituents_universe": constituent_universe,
        "company_concepts": concepts,
        "company_industry_chain": chains,
    }
    for name, implementation in targets.items():
        monkeypatch.setattr(
            f"app.providers.akshare_provider.AKShareProvider.{name}",
            implementation,
        )
    monkeypatch.setattr(
        "app.services.one_click_pipeline.refresh_company_research_if_needed",
        lambda db, symbol, **kwargs: {
            "symbol": symbol,
            "status": "fresh",
            "sections": {},
            "updated_at": datetime.now().isoformat(),
            "checked_at": datetime.now().isoformat(),
            "refreshed_sections": [],
            "missing_data": ["financials", "announcements", "valuation"],
            "freshness": [],
        },
    )


def test_one_click_empty_position_generates_and_confirms_plan(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["decision"]["label"] == "允许试仓"
    assert [item["name"] for item in data["steps"]][-7:] == [
        "行情数据",
        "公司与行业识别",
        "市场判断",
        "行业判断",
        "公司风险与公开信息",
        "个股分析",
        "风险计算",
        "AI解释",
        "生成计划",
    ][-7:]
    assert data["plan"]["account"]["max_position_pct"] == 30
    assert data["plan"]["position_calculation"]["trial_quantity"] % 100 == 0
    assert data["decision_package"]["schema_version"] == "1.1"
    assert data["decision_package"]["quality_status"] == "SINGLE_SOURCE"
    assert data["decision_package"]["strategy_decision"]["rule_status"] == "READY"
    assert data["decision_package"]["risk_decision"]["hard_stop"] == str(
        data["plan"]["buy_plan"]["hard_stop"]
    )
    saved = client.post(
        f"/api/v1/trade-plan-generator/analyze/{data['run_id']}/confirm"
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["user_confirmed"] is True
    assert saved.json()["plan_version"] == 1


def test_one_click_holding_mode_uses_inline_position(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="200000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "持仓",
            "account_id": account["id"],
            "holding_quantity": 1000,
            "holding_cost_price": 9,
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["plan"]["existing_position"]["exists"] is True
    assert data["plan"]["existing_position"]["quantity"] == 1000
    assert data["decision"]["label"] == "允许条件式加仓"
    assert "降低成本" in "；".join(data["plan"]["confirmation_add"]["prohibited"])


def test_one_click_market_down_prohibits_entry(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch, market="down", sector="down")
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    assert data["decision"]["label"] == "禁止买入"
    assert data["plan"]["market_assessment"]["risk"] == "高"
    assert data["plan"]["account"]["max_total_position_pct"] == 30


def test_one_click_creates_default_research_account(client, session, monkeypatch):
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "plan_capital": 300000,
            "enable_ai": False,
        },
    ).json()
    assert data["account"]["auto_created"] is True
    account = client.get(f"/api/v1/accounts/{data['account']['id']}").json()
    assert float(account["total_assets"]) == 300000


def test_one_click_wrapper_preserves_rule_numbers_for_same_generator_input(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    direct = generate_trade_plan_preview(
        session, TradePlanPreviewRequest(**data["generator_request"])
    )
    assert data["plan"]["status"] == direct["status"]
    assert data["plan"]["buy_plan"]["hard_stop"] == direct["buy_plan"]["hard_stop"]
    assert (
        data["plan"]["position_calculation"]["final_allowed_quantity"]
        == direct["position_calculation"]["final_allowed_quantity"]
    )
    assert (
        data["plan"]["position_calculation"]["trial_quantity"]
        == direct["position_calculation"]["trial_quantity"]
    )


@pytest.mark.parametrize("quality_status", ["STALE", "CONFLICTED", "MISSING"])
def test_untrusted_execution_data_blocks_ready_and_plan_freeze(
    quality_status, client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)

    def degraded_stock(db, symbol, provider, refresh, *, evaluated_at=None):
        del evaluated_at
        return (
            {
                "code": "market_data",
                "name": "行情数据",
                "status": "success",
                "detail": "测试质量门禁",
                "fallback_used": quality_status == "STALE",
                "missing": [],
                "quality_status": quality_status,
                "source": "test",
                "data_time": datetime.now().isoformat(),
            },
            {"data_date": date.today().isoformat(), "source": "test"},
        )

    monkeypatch.setattr("app.services.one_click_pipeline._sync_stock", degraded_stock)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["plan"]["deterministic_rule_status"] == "READY"
    assert data["plan"]["status"] == "WAIT"
    assert data["plan"]["current_buy_allowed"] is False
    assert data["decision_package"]["quality_status"] == quality_status
    assert data["decision_package"]["ready_allowed"] is False
    assert data["decision_package"]["freeze_allowed"] is False
    assert data["can_save"] is False
    confirm = client.post(
        f"/api/v1/trade-plan-generator/analyze/{data['run_id']}/confirm"
    )
    assert confirm.status_code == 422
    assert confirm.json()["error"]["code"] == "ANALYSIS_QUALITY_BLOCKED"
    assert session.query(TradePlan).count() == 0


def test_direct_save_cannot_bypass_blocked_decision_package(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)

    def missing_stock(db, symbol, provider, refresh, *, evaluated_at=None):
        del evaluated_at
        return (
            {
                "code": "market_data",
                "name": "market data",
                "status": "failed",
                "detail": "required market data missing",
                "fallback_used": False,
                "missing": ["stock_daily_bars"],
                "quality_status": "MISSING",
                "source": "test",
                "data_time": datetime.now().isoformat(),
            },
            {"data_date": date.today().isoformat(), "source": "test"},
        )

    monkeypatch.setattr("app.services.one_click_pipeline._sync_stock", missing_stock)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    assert analyzed["can_save"] is False
    direct = client.post(
        "/api/v1/trade-plan-generator/save",
        json={
            **analyzed["generator_request"],
            "preview_hash": analyzed["plan"]["preview_hash"],
        },
    )
    assert direct.status_code == 422
    assert direct.json()["error"]["code"] == "DECISION_PACKAGE_REQUIRED"
    assert session.query(TradePlan).count() == 0


def test_confirm_rechecks_quality_after_analysis_age(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    expires_at = datetime.fromisoformat(analyzed["decision_package"]["expires_at"])
    monkeypatch.setattr(
        "app.services.plan_freeze.shanghai_now",
        lambda: expires_at + timedelta(seconds=1),
    )
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_EXPIRED"


def test_confirm_rejects_changed_quality_snapshot(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    package["quality_snapshot"] = {
        **package.get("quality_snapshot", {}),
        "market_data": {"quality_status": "CONFLICTED"},
    }
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_CHANGED"


def test_confirm_rejects_required_source_change(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    announcement_scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    announcement_scan.checked_at = datetime.now() + timedelta(seconds=1)
    session.commit()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422
    assert confirmed.json()["error"]["code"] == "DECISION_PACKAGE_CHANGED"


def test_same_analysis_run_cannot_create_two_formal_plans(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    endpoint = (
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert client.post(endpoint).status_code == 201
    duplicate = client.post(endpoint)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ANALYSIS_ALREADY_CONFIRMED"
    assert session.query(TradePlan).count() == 1


def test_legacy_analysis_without_decision_package_cannot_confirm(client, session):
    account = create_account(client, assets="300000", cash="300000")
    run = PlanAnalysisRun(
        symbol="300502",
        account_id=account["id"],
        position_mode="空仓",
        status="success",
        request_snapshot={},
        pipeline_steps=[],
        result_snapshot={"plan": {"preview_hash": "a" * 64}},
    )
    session.add(run)
    session.commit()
    response = client.post(f"/api/v1/trade-plan-generator/analyze/{run.id}/confirm")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DECISION_PACKAGE_REQUIRED"


def test_required_announcements_missing_blocks_freeze(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    session.query(CompanyAnnouncement).delete()
    session.query(CompanyResearchRefresh).filter_by(section="announcements").delete()
    session.commit()
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    assert data["decision_package"]["strategy_decision"]["rule_status"] == "READY"
    assert data["decision_package"]["strategy_decision"]["executable_status"] == "WAIT"
    assert data["decision_package"]["freeze_allowed"] is False
    assert data["decision_package"]["quality_status"] == "MISSING"


def test_optional_financials_missing_does_not_change_rule_status(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    data = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    package = data["decision_package"]
    assert package["strategy_decision"]["rule_status"] == "READY"
    assert package["freeze_allowed"] is True
    assert "financials" in package["research_decision"]["missing_optional_evidence"]


def _holding_analysis_with_quote_quality(
    client, session, monkeypatch, quality_status: str | None, quote_type="realtime"
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    quote = session.query(MarketQuote).one()
    if quality_status is None:
        session.delete(quote)
    elif quality_status == "STALE":
        stale_at = datetime.now() - timedelta(minutes=31)
        quote.observed_at = stale_at
        record = session.get(DataQualityRecord, quote.quality_record_id)
        record.observed_at = stale_at
    elif quality_status == "CONFLICTED":
        registry = ProviderRegistry()
        registry.register(QuoteScenarioProvider("quote-a", price="10.82"))
        registry.register(
            QuoteScenarioProvider("quote-b", price="11.82", priority=2)
        )
        DataHubRouter(session, registry).get_quote("300502")
    elif quote_type == "latest_close":
        session.delete(quote)
        session.flush()
        registry = ProviderRegistry()
        registry.register(
            QuoteScenarioProvider("latest-close", quote_type="latest_close")
        )
        router = DataHubRouter(session, registry)
        result = router.get_latest_close("300502")
        persist_market_quote(session, router, result)
    session.commit()
    return client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "持仓",
            "account_id": account["id"],
            "holding_quantity": 1000,
            "holding_cost_price": "9.8",
            "enable_ai": False,
        },
    ).json()


def test_stale_quote_blocks_holding_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "STALE"
    )
    assert data["decision"]["status"] == "WAIT"
    assert data["decision_package"]["freeze_allowed"] is False


def test_conflicted_quote_blocks_holding_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "CONFLICTED"
    )
    assert data["decision"]["status"] == "WAIT"


def test_missing_quote_blocks_price_triggered_decision(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(client, session, monkeypatch, None)
    assert data["decision"]["status"] == "WAIT"


def test_quote_fallback_close_is_not_treated_as_realtime(
    client, session, monkeypatch
):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "SINGLE_SOURCE", quote_type="latest_close"
    )
    evidence = next(
        item
        for item in data["decision_package"]["evidence"]
        if item["capability"] == "market_quote"
    )
    assert evidence["payload"]["quote_type"] == "latest_close"
    assert evidence["payload"]["fallback_used"] is True


def test_fresh_daily_bar_does_not_hide_stale_quote(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "STALE"
    )
    qualities = {
        item["capability"]: item["quality_status"]
        for item in data["decision_package"]["evidence"]
    }
    assert qualities["stock_daily_bars"] == "SINGLE_SOURCE"
    assert qualities["market_quote"] == "STALE"


def test_market_quote_is_required_evidence(client, session, monkeypatch):
    data = _holding_analysis_with_quote_quality(
        client, session, monkeypatch, "SINGLE_SOURCE"
    )
    package = data["decision_package"]
    assert "market_quote" in package["required_capabilities"]
    assert any(
        item["capability"] == "market_quote" and item["required"]
        for item in package["evidence"]
    )


def _analyze_bound_plan(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _analyze_with_research_near_boundaries(client, session, monkeypatch):
    current = datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    _patch_market_clock(
        monkeypatch,
        current.astimezone(timezone.utc),
    )
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile_with_research_times(
        session,
        profile_at=current - timedelta(days=30) + timedelta(minutes=1),
        announcement_at=current - timedelta(hours=24) + timedelta(minutes=1),
        evaluated_at=current,
    )
    patch_benchmarks(monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), current


def _confirm_bound_plan(client, analyzed):
    return client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )


def test_confirm_rejects_package_when_market_closes_before_confirmation(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    assert analyzed["decision_package"]["freeze_allowed"] is True
    monkeypatch.setattr(
        XSHGTradingCalendar,
        "market_phase",
        lambda self, now=None: TradingPhase.CLOSED,
    )

    response = _confirm_bound_plan(client, analyzed)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"


def test_active_session_fresh_realtime_quote_can_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)

    response = _confirm_bound_plan(client, analyzed)

    assert response.status_code == 201, response.text
    assert session.query(TradePlan).filter_by(
        analysis_run_id=analyzed["run_id"]
    ).count() == 1


def _patch_market_clock(monkeypatch, instant):
    shanghai_instant = instant.astimezone(SHANGHAI_TZ)
    monkeypatch.setattr(
        "app.data_hub.trading_calendar.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr(
        "app.data_hub.research_subjects.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr("app.data_hub.router.shanghai_now", lambda: shanghai_instant)
    monkeypatch.setattr(
        "app.data_hub.effective_quality.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr(
        "app.providers.akshare_provider.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr("app.services.plan_freeze.shanghai_now", lambda: instant)
    monkeypatch.setattr(
        "app.services.one_click_pipeline.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr(
        "app.services.trade_plan_generator.shanghai_now", lambda: shanghai_instant
    )
    monkeypatch.setattr(
        "app.services.trade_plan_generator.shanghai_today",
        lambda: shanghai_instant.date(),
    )
    monkeypatch.setattr(__name__ + ".shanghai_now", lambda: shanghai_instant)


def test_freeze_market_validation_uses_shanghai_clock(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    evaluated_at_values = []
    from app.services import plan_freeze

    resolver = plan_freeze.resolve_market_quality_binding

    def capture_clock(db, binding, *, evaluated_at=None):
        evaluated_at_values.append(evaluated_at)
        return resolver(db, binding, evaluated_at=evaluated_at)

    monkeypatch.setattr(plan_freeze, "resolve_market_quality_binding", capture_clock)
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 201, response.text
    assert evaluated_at_values
    assert all(
        value.astimezone(SHANGHAI_TZ)
        == datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
        for value in evaluated_at_values
    )


def test_confirm_at_utc_0200_is_treated_as_shanghai_morning(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 201, response.text


def test_confirm_at_utc_0700_is_treated_as_shanghai_close(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    utc_close = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.plan_freeze.shanghai_now", lambda: utc_close)
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"


def test_decision_package_times_are_shanghai_aware(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = analyzed["decision_package"]
    for field in ("created_at", "generated_at", "expires_at"):
        value = datetime.fromisoformat(package[field])
        assert value.utcoffset() == timedelta(hours=8)


def test_package_expiry_is_exactly_24_hours(client, session, monkeypatch):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    package = _analyze_bound_plan(client, session, monkeypatch)["decision_package"]
    generated_at = datetime.fromisoformat(package["generated_at"])
    expires_at = datetime.fromisoformat(package["expires_at"])
    assert expires_at - generated_at == timedelta(hours=24)


def test_package_expiry_does_not_shorten_on_utc_host(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    package = _analyze_bound_plan(client, session, monkeypatch)["decision_package"]
    assert datetime.fromisoformat(package["generated_at"]) == datetime(
        2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ
    )
    assert datetime.fromisoformat(package["expires_at"]) == datetime(
        2026, 7, 25, 10, 0, tzinfo=SHANGHAI_TZ
    )


def test_confirm_package_age_is_host_timezone_independent(
    client, session, monkeypatch
):
    utc_morning = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, utc_morning)
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    run.created_at = datetime(2026, 7, 23, 2, 0)
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 201, response.text


def test_one_click_propagates_one_analysis_started_at(
    client, session, monkeypatch
):
    from app.services import one_click_pipeline

    instant = datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc)
    expected = instant.astimezone(SHANGHAI_TZ)
    _patch_market_clock(monkeypatch, instant)
    captured = {}
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    real_refresh = one_click_pipeline.refresh_company_research_if_needed
    real_inventory = one_click_pipeline._research_inventory
    real_preview = one_click_pipeline.generate_trade_plan_preview

    def capture_refresh(*args, evaluated_at=None, **kwargs):
        captured["refresh"] = evaluated_at
        return real_refresh(*args, evaluated_at=evaluated_at, **kwargs)

    def capture_inventory(*args, evaluated_at=None, **kwargs):
        captured["inventory"] = evaluated_at
        return real_inventory(*args, evaluated_at=evaluated_at, **kwargs)

    def capture_preview(*args, evaluated_at=None, **kwargs):
        captured["preview"] = evaluated_at
        return real_preview(*args, evaluated_at=evaluated_at, **kwargs)

    monkeypatch.setattr(
        one_click_pipeline, "refresh_company_research_if_needed", capture_refresh
    )
    monkeypatch.setattr(one_click_pipeline, "_research_inventory", capture_inventory)
    monkeypatch.setattr(
        one_click_pipeline, "generate_trade_plan_preview", capture_preview
    )
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    assert captured == {
        "refresh": expected,
        "inventory": expected,
        "preview": expected,
    }


def test_los_angeles_host_one_click_uses_shanghai_announcement_scope_and_confirms(
    client, session, monkeypatch
):
    instant = datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, instant)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    from app.services import one_click_pipeline

    real_refresh = one_click_pipeline.refresh_company_research_if_needed
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    seed_optional_research_cache(session, instant)
    patch_benchmarks(monkeypatch)
    monkeypatch.setattr(
        one_click_pipeline, "refresh_company_research_if_needed", real_refresh
    )
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    analyzed = response.json()
    evidence = _research_evidence(analyzed)["announcements"]
    binding = evidence["source_quality_binding"]
    assert binding["semantic_key"].endswith("/2026-07-25")
    assert datetime.fromisoformat(binding["scan_end"]).date() == date(2026, 7, 25)
    assert all(
        datetime.fromisoformat(value).tzinfo is not None
        for value in (
            evidence["observed_at"],
            evidence["fetched_at"],
        )
    )
    assert datetime.fromisoformat(evidence["observed_at"]).astimezone(
        timezone.utc
    ).replace(tzinfo=None) == datetime.fromisoformat(binding["observed_at"])
    confirm = _confirm_bound_plan(client, analyzed)
    assert confirm.status_code == 201, confirm.text


def test_evidence_and_package_hashes_do_not_depend_on_host_timezone(
    client, session, monkeypatch
):
    instant = datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc)
    _patch_market_clock(monkeypatch, instant)
    from app.services import one_click_pipeline

    real_refresh = one_click_pipeline.refresh_company_research_if_needed
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    seed_optional_research_cache(session, instant)
    patch_benchmarks(monkeypatch)
    monkeypatch.setattr(
        one_click_pipeline, "refresh_company_research_if_needed", real_refresh
    )
    payload = {
        "symbol": "300502",
        "position_mode": "空仓",
        "account_id": account["id"],
        "enable_ai": False,
    }
    first_response = client.post(
        "/api/v1/trade-plan-generator/analyze", json=payload
    )
    assert first_response.status_code == 200, first_response.text
    analyzed = first_response.json()
    from app.domain.package_builder import build_decision_package

    build_kwargs = {
        "preview": analyzed["plan"],
        "decision": analyzed["decision"],
        "steps": [
            step for step in analyzed["steps"] if step["code"] != "plan_output"
        ],
        "ai_result": analyzed["ai"],
        "orchestrator_id": analyzed["decision_package"]["research_decision"][
            "orchestrator"
        ],
    }
    first_package = build_decision_package(**build_kwargs)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    second_package = build_decision_package(**build_kwargs)
    assert first_package.evidence_digest == second_package.evidence_digest
    assert first_package.package_hash == second_package.package_hash


def test_formal_announcement_chain_has_no_host_local_clock_calls():
    from app.data_hub.research_subjects import announcement_catalog_window
    from app.services.company_research import (
        refresh_company_research_if_needed,
        sync_company_research,
    )
    from app.services.one_click_pipeline import _research_inventory

    for function in (
        announcement_catalog_window,
        sync_company_research,
        refresh_company_research_if_needed,
        _research_inventory,
    ):
        source = python_inspect.getsource(function)
        assert "datetime.now(" not in source
        assert "date.today(" not in source


def test_missing_research_attempt_still_allows_fresh_bound_cache_near_boundary(
    client, session, monkeypatch
):
    analyzed, current = _analyze_with_research_near_boundaries(
        client,
        session,
        monkeypatch,
    )
    evidence = _research_evidence(analyzed)["announcements"]
    binding = evidence["source_quality_binding"]
    registry = ProviderRegistry()
    registry.register(
        ResearchBindingScenarioProvider(
            "missing-near-boundary",
            fail=True,
        )
    )
    router = DataHubRouter(session, registry, now_fn=lambda: current)
    missing = router.company_announcements(
        "300502",
        datetime.fromisoformat(binding["scan_start"]).date(),
        datetime.fromisoformat(binding["scan_end"]).date(),
    )
    assert missing.quality_status.value == "MISSING"
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 201, response.text


def test_source_binding_confirm_near_freshness_boundary(
    client, session, monkeypatch
):
    analyzed, _ = _analyze_with_research_near_boundaries(
        client,
        session,
        monkeypatch,
    )
    research = _research_evidence(analyzed)
    assert research["company_profile"]["source_quality_binding"]
    assert research["announcements"]["source_quality_binding"]
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 201, response.text


def _research_evidence(analyzed):
    return {
        item["capability"]: item
        for item in analyzed["decision_package"]["evidence"]
        if item["capability"] in {"company_profile", "announcements"}
    }


def test_profile_evidence_contains_exact_source_binding(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    evidence = _research_evidence(analyzed)["company_profile"]
    binding = evidence["source_quality_binding"]
    profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert binding["data_capability"] == "fundamental.profile"
    assert binding["quality_record_id"] == profile.quality_record_id
    assert binding["semantic_key"] == "profile"


def test_announcement_evidence_contains_exact_source_binding(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    evidence = _research_evidence(analyzed)["announcements"]
    binding = evidence["source_quality_binding"]
    refresh = session.query(CompanyResearchRefresh).filter_by(
        symbol="300502", section="announcements"
    ).one()
    assert binding["data_capability"] == "announcement.catalog"
    assert binding["quality_record_id"] == refresh.quality_record_id
    assert binding["checked_at"] == binding["observed_at"]


def test_source_binding_is_included_in_evidence_digest(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = DecisionPackage.model_validate(analyzed["decision_package"])
    evidence = list(package.evidence)
    target = next(item for item in evidence if item.capability == "company_profile")
    changed_binding = target.source_quality_binding.model_copy(
        update={"quality_record_id": target.source_quality_binding.quality_record_id + 1}
    )
    evidence[evidence.index(target)] = target.model_copy(
        update={"source_quality_binding": changed_binding}
    )
    changed = package.model_copy(update={"evidence": evidence})
    assert changed.evidence_digest_value() != package.evidence_digest


def test_source_binding_is_included_in_package_hash(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = DecisionPackage.model_validate(analyzed["decision_package"])
    evidence = list(package.evidence)
    target = next(item for item in evidence if item.capability == "company_profile")
    changed_binding = target.source_quality_binding.model_copy(
        update={"quality_record_id": target.source_quality_binding.quality_record_id + 1}
    )
    evidence[evidence.index(target)] = target.model_copy(
        update={"source_quality_binding": changed_binding}
    )
    changed = package.model_copy(update={"evidence": evidence})
    assert changed.package_hash_value() != package.package_hash


@pytest.mark.parametrize("capability", ["company_profile", "announcements"])
def test_flat_source_fields_cannot_create_binding(
    client, session, monkeypatch, capability
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    payload = analyzed["decision_package"]
    target = next(item for item in payload["evidence"] if item["capability"] == capability)
    binding = target.pop("source_quality_binding")
    target.update(binding)
    with pytest.raises(ValueError):
        DecisionPackage.model_validate(payload)


def test_legacy_package_without_source_binding_cannot_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    package["evidence"] = [dict(item) for item in package["evidence"]]
    for item in package["evidence"]:
        if item["capability"] in {"company_profile", "announcements"}:
            item.pop("source_quality_binding", None)
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DECISION_PACKAGE_SOURCE_BINDING_REQUIRED"


def _add_research_conflict(session, capability):
    today = shanghai_today()
    announcements = [
        {
            "公告标题": "冲突公告",
            "公告日期": today.isoformat(),
            "公告链接": "https://example.test/conflict",
        }
    ]
    router = _router_for_binding(
        session,
        ResearchBindingScenarioProvider("research-a", priority=1),
        ResearchBindingScenarioProvider(
            "research-b",
            profile_name="不同公司",
            announcements=announcements,
            priority=2,
        ),
    )
    if capability == "company_profile":
        result = router.company_profile("300502")
    else:
        result = router.company_announcements(
            "300502", today - timedelta(days=3 * 366), today
        )
    assert result.quality_status.value == "CONFLICTED"
    session.commit()


@pytest.mark.parametrize("capability", ["company_profile", "announcements"])
def test_research_conflict_after_analysis_blocks_confirm(
    client, session, monkeypatch, capability
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_research_conflict(session, capability)
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert session.query(TradePlan).count() == 0


def test_missing_research_attempt_does_not_block_fresh_bound_cache(
    client, session, monkeypatch
):
    today = shanghai_today()
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    router = _router_for_binding(
        session, ResearchBindingScenarioProvider("missing", fail=True)
    )
    assert router.company_profile("300502").quality_status.value == "MISSING"
    assert router.company_announcements(
        "300502", today - timedelta(days=3 * 366), today
    ).quality_status.value == "MISSING"
    session.commit()
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_empty_scan_can_freeze_formal_plan(client, session, monkeypatch):
    today = shanghai_today()
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    router = _router_for_binding(
        session,
        ResearchBindingScenarioProvider("empty-catalog", announcements=[]),
    )
    profile_result = router.company_profile("300502")
    persist_company_profile(session, router, profile_result)
    start, end = today - timedelta(days=3 * 366), today
    catalog_result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(
        session, router, catalog_result, start=start, end=end
    )
    session.commit()
    patch_benchmarks(monkeypatch)
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    company_risk = next(
        step for step in analyzed["steps"] if step["code"] == "company_risk"
    )
    assert "扫描成功" in company_risk["detail"]
    assert _confirm_bound_plan(client, analyzed).status_code == 201


@pytest.mark.parametrize("capability", ["company_profile", "announcements"])
def test_new_persisted_research_lineage_requires_reanalysis(
    client, session, monkeypatch, capability
):
    today = shanghai_today()
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    router = _router_for_binding(
        session,
        ResearchBindingScenarioProvider(
            "research-replacement",
            profile_name="替换公司",
            announcements=[],
        ),
    )
    if capability == "company_profile":
        result = router.company_profile("300502")
        persist_company_profile(session, router, result)
    else:
        start, end = today - timedelta(days=3 * 366), today
        result = router.company_announcements("300502", start, end)
        persist_announcement_catalog(
            session, router, result, start=start, end=end
        )
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SOURCE_BINDING_CHANGED"


def _router_for_binding(session, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry)


def _add_market_conflict(session, capability):
    router = _router_for_binding(
        session,
        BindingScenarioProvider("binding-a", close="10", quote_price="10.82"),
        BindingScenarioProvider(
            "binding-b", close="11", quote_price="11.82", priority=2
        ),
    )
    if capability == "stock_daily_bars":
        result = router.get_history(
            "300502", date.today() - timedelta(days=365), date.today()
        )
    elif capability == "market_quote":
        result = router.get_quote("300502")
    elif capability == "benchmark_daily_bars":
        result = router.get_index_history(
            "csi000300", date.today() - timedelta(days=240), date.today()
        )
    else:
        profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
        result = router.get_sector_history(
            profile.industry, date.today() - timedelta(days=240), date.today()
        )
    assert result.quality_status.value == "CONFLICTED"
    session.commit()


def _replace_bound_series(session, capability):
    router = _router_for_binding(
        session, BindingScenarioProvider(f"replacement-{capability}", close="12")
    )
    if capability == "stock_daily_bars":
        result = router.get_history(
            "300502", date.today() - timedelta(days=365), date.today()
        )
        bars = result.require_value()
        min_rows = 250
    elif capability == "benchmark_daily_bars":
        result = router.get_index_history(
            "csi000300", date.today() - timedelta(days=240), date.today()
        )
        bars = mapping_series_bars(
            result.require_value(),
            cache_symbol=result.subject.subject_id,
            adjustment="unadjusted",
        )
        min_rows = 60
    else:
        profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
        result = router.get_sector_history(
            profile.industry, date.today() - timedelta(days=240), date.today()
        )
        bars = mapping_series_bars(
            result.require_value(),
            cache_symbol=result.subject.subject_id,
            adjustment="unadjusted",
        )
        min_rows = 60
    session.commit()
    replace_market_series(
        session, router, result, bars, subject=result.subject, min_rows=min_rows
    )
    session.commit()


def _rewrite_package_binding(session, analyzed, capability, **updates):
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = DecisionPackage.model_validate(snapshot["decision_package"])
    evidence = []
    for item in package.evidence:
        if item.capability == capability:
            binding = item.market_quality_binding.model_copy(update=updates)
            item_updates = {"market_quality_binding": binding}
            if "observed_at" in updates:
                item_updates["observed_at"] = binding.observed_at.isoformat()
            item = item.model_copy(update=item_updates)
        evidence.append(item)
    package_updates = {"evidence": evidence}
    if "observed_at" in updates:
        quality_snapshot = dict(package.quality_snapshot)
        quality_snapshot[capability] = quality_snapshot[capability].model_copy(
            update={"observed_at": [binding.observed_at.isoformat()]}
        )
        package_updates["quality_snapshot"] = quality_snapshot
    changed = package.model_copy(update=package_updates)
    changed = changed.model_copy(
        update={"evidence_digest": changed.evidence_digest_value()}
    )
    payload = changed.model_dump(mode="json")
    payload["package_hash"] = changed.package_hash_value()
    snapshot["decision_package"] = DecisionPackage.model_validate(payload).model_dump(
        mode="json"
    )
    run.result_snapshot = snapshot
    session.commit()


def test_market_evidence_contains_exact_quality_binding(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    evidence = {
        item["capability"]: item
        for item in analyzed["decision_package"]["evidence"]
        if item["capability"] in MARKET_EVIDENCE_CAPABILITIES
    }
    assert set(evidence) == set(MARKET_EVIDENCE_CAPABILITIES)
    for capability, data_capability in MARKET_EVIDENCE_CAPABILITIES.items():
        binding = evidence[capability]["market_quality_binding"]
        assert binding["data_capability"] == data_capability
        assert binding["quality_record_id"] > 0
        assert binding["observed_at"]
        record = session.get(DataQualityRecord, binding["quality_record_id"])
        assert record.persisted is True
        assert record.subject_type == binding["subject_type"]
        assert record.subject_id == binding["subject_id"]


def test_confirm_revalidates_stock_daily_binding(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _rewrite_package_binding(
        session, analyzed, "stock_daily_bars", subject_id="300503"
    )
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_confirm_revalidates_realtime_quote_binding(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = DecisionPackage.model_validate(analyzed["decision_package"])
    binding = next(
        item.market_quality_binding
        for item in package.evidence
        if item.capability == "market_quote"
    )
    _rewrite_package_binding(
        session,
        analyzed,
        "market_quote",
        observed_at=binding.observed_at + timedelta(seconds=1),
    )
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_confirm_revalidates_index_binding(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _rewrite_package_binding(
        session, analyzed, "benchmark_daily_bars", subject_id="CSI000905"
    )
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_confirm_revalidates_sector_binding(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _rewrite_package_binding(
        session, analyzed, "sector_daily_bars", subject_id="f" * 64
    )
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_stock_daily_conflict_after_analysis_blocks_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_market_conflict(session, "stock_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"
    assert session.query(TradePlan).count() == 0


def test_quote_conflict_after_analysis_blocks_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_market_conflict(session, "market_quote")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"
    assert session.query(TradePlan).count() == 0


def test_index_conflict_after_analysis_blocks_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_market_conflict(session, "benchmark_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"
    assert session.query(TradePlan).count() == 0


def test_sector_conflict_after_analysis_blocks_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_market_conflict(session, "sector_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"
    assert session.query(TradePlan).count() == 0


def test_new_persisted_stock_lineage_requires_reanalysis(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _replace_bound_series(session, "stock_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_CHANGED"
    assert session.query(TradePlan).count() == 0


def test_new_persisted_index_lineage_requires_reanalysis(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _replace_bound_series(session, "benchmark_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_CHANGED"
    assert session.query(TradePlan).count() == 0


def test_new_persisted_sector_lineage_requires_reanalysis(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _replace_bound_series(session, "sector_daily_bars")
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_CHANGED"
    assert session.query(TradePlan).count() == 0


def test_quote_naturally_ages_to_stale_before_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    quote = session.query(MarketQuote).one()
    stale_at = datetime.now() - timedelta(minutes=31)
    quote.observed_at = stale_at
    session.get(DataQualityRecord, quote.quality_record_id).observed_at = stale_at
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"


def test_latest_close_cannot_replace_realtime_binding(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    registry = ProviderRegistry()
    registry.register(QuoteScenarioProvider("latest-only", quote_type="latest_close"))
    router = DataHubRouter(session, registry)
    result = router.get_latest_close("300502")
    persist_market_quote(session, router, result)
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_NOT_EXECUTABLE"


def test_missing_refresh_attempt_does_not_block_fresh_bound_cache(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    router = _router_for_binding(
        session, BindingScenarioProvider("failed-refresh", fail=True)
    )
    result = router.get_history(
        "300502", date.today() - timedelta(days=365), date.today()
    )
    assert result.quality_status.value == "MISSING"
    session.commit()
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_unrelated_stock_conflict_does_not_block_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    router = _router_for_binding(
        session,
        BindingScenarioProvider("other-stock-a", close="10"),
        BindingScenarioProvider("other-stock-b", close="11", priority=2),
    )
    result = router.get_history(
        "300503", date.today() - timedelta(days=365), date.today()
    )
    assert result.quality_status.value == "CONFLICTED"
    session.commit()
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_unrelated_sector_conflict_does_not_block_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    router = _router_for_binding(
        session,
        BindingScenarioProvider("other-sector-a", close="10"),
        BindingScenarioProvider("other-sector-b", close="11", priority=2),
    )
    result = router.get_sector_history(
        "unrelated-sector", date.today() - timedelta(days=240), date.today()
    )
    assert result.quality_status.value == "CONFLICTED"
    session.commit()
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_binding_scope_mismatch_blocks_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _rewrite_package_binding(
        session, analyzed, "stock_daily_bars", subject_id="300503"
    )
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_SCOPE_MISMATCH"


def test_binding_observed_at_mismatch_blocks_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    package = DecisionPackage.model_validate(analyzed["decision_package"])
    binding = next(
        item.market_quality_binding
        for item in package.evidence
        if item.capability == "stock_daily_bars"
    )
    _rewrite_package_binding(
        session,
        analyzed,
        "stock_daily_bars",
        observed_at=binding.observed_at + timedelta(seconds=1),
    )
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_BINDING_CHANGED"


def test_binding_quality_record_id_mismatch_blocks_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    quote_record_id = next(
        item["market_quality_binding"]["quality_record_id"]
        for item in analyzed["decision_package"]["evidence"]
        if item["capability"] == "market_quote"
    )
    _rewrite_package_binding(
        session,
        analyzed,
        "stock_daily_bars",
        quality_record_id=quote_record_id,
    )
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422


def test_tampered_binding_rejects_confirm(client, session, monkeypatch):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    evidence = [dict(item) for item in package["evidence"]]
    target = next(item for item in evidence if item["capability"] == "market_quote")
    target["market_quality_binding"] = dict(target["market_quality_binding"])
    target["market_quality_binding"]["quality_record_id"] += 1
    package["evidence"] = evidence
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DECISION_PACKAGE_CHANGED"


def test_legacy_package_without_market_bindings_cannot_confirm(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    package["evidence"] = [
        {key: value for key, value in item.items() if key != "market_quality_binding"}
        for item in package["evidence"]
    ]
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "DECISION_PACKAGE_MARKET_BINDING_REQUIRED"
    )


def test_failed_binding_validation_creates_no_trade_plan(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    _add_market_conflict(session, "benchmark_daily_bars")
    assert _confirm_bound_plan(client, analyzed).status_code == 422
    assert session.query(TradePlan).count() == 0


def test_direct_save_cannot_bypass_market_binding_validation(
    client, session, monkeypatch
):
    analyzed = _analyze_bound_plan(client, session, monkeypatch)
    response = client.post(
        "/api/v1/trade-plan-generator/save",
        json={
            **analyzed["generator_request"],
            "preview_hash": analyzed["plan"]["preview_hash"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DECISION_PACKAGE_REQUIRED"
    assert session.query(TradePlan).count() == 0


def _refresh_analysis_with_quote_scenario(
    client,
    session,
    monkeypatch,
    *,
    quote_mode="missing",
    remove_cached_quote=False,
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    old_quote = session.query(MarketQuote).one()
    old_record_id = old_quote.quality_record_id
    old_observed_at = old_quote.observed_at
    cached_rows = session.query(MarketDailyBar).filter_by(symbol="300502").all()
    if remove_cached_quote:
        session.delete(old_quote)
        session.commit()

    if quote_mode == "conflicted":
        providers = (
            PatternRefreshProvider(
                "refresh-a", cached_rows, close="10", quote_price="10.82"
            ),
            PatternRefreshProvider(
                "refresh-b",
                cached_rows,
                close="10",
                quote_price="11.82",
                priority=2,
            ),
        )
    else:
        providers = (
            PatternRefreshProvider(
                "refresh-missing",
                cached_rows,
                close="10",
                quote_failure=True,
            ),
        )
    router = _router_for_binding(session, *providers)
    monkeypatch.setattr(
        "app.services.one_click_pipeline.build_data_hub",
        lambda db, **kwargs: router,
    )
    response = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "refresh": True,
            "enable_ai": False,
        },
    )
    assert response.status_code == 200, response.text
    return response.json(), old_record_id, old_observed_at


def _quote_evidence(analyzed):
    return next(
        item
        for item in analyzed["decision_package"]["evidence"]
        if item["capability"] == "market_quote"
    )


def test_refresh_quote_failure_uses_existing_fresh_realtime_binding(
    client, session, monkeypatch
):
    analyzed, old_record_id, old_observed_at = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    evidence = _quote_evidence(analyzed)
    binding = evidence["market_quality_binding"]
    assert binding["quality_record_id"] == old_record_id
    assert datetime.fromisoformat(binding["observed_at"]) == old_observed_at
    assert evidence["payload"]["quote_type"] == "realtime"
    assert evidence["payload"]["fallback_used"] is True
    missing_attempt = session.query(DataQualityRecord).filter_by(
        capability="market.quote.realtime", quality_status="MISSING"
    ).one()
    assert missing_attempt.persisted is False
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_market_quote_binding_comes_from_selected_quote(
    client, session, monkeypatch
):
    analyzed, old_record_id, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    assert _quote_evidence(analyzed)["market_quality_binding"][
        "quality_record_id"
    ] == old_record_id


def test_quote_binding_observed_at_matches_quality_record(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    binding = _quote_evidence(analyzed)["market_quality_binding"]
    record = session.get(DataQualityRecord, binding["quality_record_id"])
    assert datetime.fromisoformat(binding["observed_at"]) == record.observed_at


def test_quote_binding_payload_and_lineage_use_same_quote(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    evidence = _quote_evidence(analyzed)
    assert evidence["payload"]["quote_type"] == "realtime"
    assert evidence["payload"]["execution_quote_type"] == "realtime"
    assert evidence["payload"]["execution_price"] == evidence["payload"]["price"]
    assert evidence["observed_at"] == evidence["market_quality_binding"]["observed_at"]


def test_latest_close_display_has_no_realtime_execution_binding(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch, remove_cached_quote=True
    )
    evidence = _quote_evidence(analyzed)
    assert evidence["market_quality_binding"] is None
    assert evidence["payload"]["display_quote_type"] == "latest_close"
    assert evidence["payload"]["execution_quote_type"] is None
    assert analyzed["decision_package"]["freeze_allowed"] is False


def test_missing_quote_result_does_not_report_realtime_refresh_success(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch, remove_cached_quote=True
    )
    step = next(item for item in analyzed["steps"] if item["code"] == "market_quote")
    assert step["status"] == "partial"
    assert "已取得实时价格" not in step["detail"]


def test_refresh_failure_with_no_trusted_quote_blocks_freeze(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch, remove_cached_quote=True
    )
    assert analyzed["decision_package"]["freeze_allowed"] is False
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_refresh_failure_with_fresh_quote_allows_confirm(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    assert analyzed["decision_package"]["freeze_allowed"] is True
    assert _confirm_bound_plan(client, analyzed).status_code == 201


def test_refresh_conflict_still_blocks_confirm(client, session, monkeypatch):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch, quote_mode="conflicted"
    )
    assert analyzed["decision_package"]["freeze_allowed"] is False
    assert _confirm_bound_plan(client, analyzed).status_code == 422


def test_all_four_market_steps_emit_explicit_binding(client, session, monkeypatch):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    market_codes = {
        "market_data",
        "market_quote",
        "market_judgement",
        "industry_judgement",
    }
    steps = {item["code"]: item for item in analyzed["steps"]}
    assert all(steps[code]["market_quality_binding"] for code in market_codes)


def test_removing_nested_binding_blocks_freeze_even_if_flat_fields_exist(
    client, session, monkeypatch
):
    analyzed, _, _ = _refresh_analysis_with_quote_scenario(
        client, session, monkeypatch
    )
    run = session.get(PlanAnalysisRun, analyzed["run_id"])
    quote_step = next(
        item for item in run.pipeline_steps if item["code"] == "market_quote"
    )
    assert quote_step["subject_id"] == "300502"
    assert quote_step["quality_record_id"] > 0
    snapshot = dict(run.result_snapshot)
    package = dict(snapshot["decision_package"])
    evidence = [dict(item) for item in package["evidence"]]
    market_quote = next(
        item for item in evidence if item["capability"] == "market_quote"
    )
    market_quote["market_quality_binding"] = None
    package["evidence"] = evidence
    snapshot["decision_package"] = package
    run.result_snapshot = snapshot
    session.commit()
    response = _confirm_bound_plan(client, analyzed)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "DECISION_PACKAGE_MARKET_BINDING_REQUIRED"
    )
    assert session.query(TradePlan).count() == 0


def test_old_latest_announcement_with_fresh_catalog_scan_can_confirm(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    session.query(CompanyAnnouncement).one().published_date = date.today() - timedelta(
        days=30
    )
    session.commit()
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 201, confirmed.text


def test_stale_announcement_catalog_blocks_confirm(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    scan.checked_at = datetime.now() - timedelta(days=2)
    scan.quality_status = "STALE"
    session.commit()
    analyzed = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()
    confirmed = client.post(
        f"/api/v1/trade-plan-generator/analyze/{analyzed['run_id']}/confirm"
    )
    assert confirmed.status_code == 422


def test_announcement_published_date_does_not_control_catalog_freshness(
    client, session, monkeypatch
):
    test_old_latest_announcement_with_fresh_catalog_scan_can_confirm(
        client, session, monkeypatch
    )


def _concurrent_database(tmp_path, monkeypatch, run_count=1):
    database = tmp_path / f"confirm-{run_count}.db"
    engine = create_engine(
        f"sqlite:///{database}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    patch_benchmarks(monkeypatch)
    with SessionLocal() as db:
        seed_pattern(db)
        seed_profile(db)
        first = run_one_click_analysis(
            db,
            OneClickPlanRequest(
                symbol="300502",
                position_mode="空仓",
                enable_ai=False,
            ),
        )
        run_ids = [first["run_id"]]
        for _ in range(run_count - 1):
            run_ids.append(
                run_one_click_analysis(
                    db,
                    OneClickPlanRequest(
                        symbol="300502",
                        position_mode="空仓",
                        account_id=first["account"]["id"],
                        enable_ai=False,
                    ),
                )["run_id"]
            )
    return engine, SessionLocal, run_ids


def _confirm_concurrently(SessionLocal, run_ids):
    barrier = Barrier(len(run_ids))
    lock = Lock()
    results = []

    def worker(run_id):
        with SessionLocal() as db:
            try:
                barrier.wait()
                saved = confirm_one_click_plan(db, run_id)
                outcome = ("ok", saved["id"], saved["plan_version"])
            except AppError as exc:
                outcome = ("error", exc.code)
            with lock:
                results.append(outcome)

    threads = [Thread(target=worker, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return results


def test_concurrent_confirm_sqlite_creates_exactly_one_plan(tmp_path, monkeypatch):
    _, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    results = _confirm_concurrently(SessionLocal, [run_ids[0], run_ids[0]])
    assert [item[0] for item in results].count("ok") == 1
    assert ("error", "ANALYSIS_ALREADY_CONFIRMED") in results
    with SessionLocal() as db:
        assert db.query(TradePlan).count() == 1


def test_concurrent_confirm_two_sessions_is_idempotent(tmp_path, monkeypatch):
    test_concurrent_confirm_sqlite_creates_exactly_one_plan(tmp_path, monkeypatch)


def test_concurrent_plan_version_allocation_is_unique(tmp_path, monkeypatch):
    _, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch, run_count=2)
    results = _confirm_concurrently(SessionLocal, run_ids)
    assert all(item[0] == "ok" for item in results)
    assert {item[2] for item in results} == {1, 2}


def test_analysis_run_id_database_uniqueness(tmp_path, monkeypatch):
    engine, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    with SessionLocal() as db:
        confirm_one_click_plan(db, run_ids[0])
        plan = db.query(TradePlan).one()
        values = {
            column.name: getattr(plan, column.name)
            for column in TradePlan.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        values["plan_version"] += 1
        with pytest.raises(IntegrityError):
            db.execute(insert(TradePlan).values(**values))
            db.commit()
    names = {item["name"] for item in inspect(engine).get_unique_constraints("trade_plans")}
    assert "uq_trade_plan_analysis_run" in names


def test_plan_version_database_uniqueness(tmp_path, monkeypatch):
    engine, SessionLocal, run_ids = _concurrent_database(tmp_path, monkeypatch)
    with SessionLocal() as db:
        confirm_one_click_plan(db, run_ids[0])
        plan = db.query(TradePlan).one()
        values = {
            column.name: getattr(plan, column.name)
            for column in TradePlan.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        values["analysis_run_id"] = None
        with pytest.raises(IntegrityError):
            db.execute(insert(TradePlan).values(**values))
            db.commit()
    names = {item["name"] for item in inspect(engine).get_unique_constraints("trade_plans")}
    assert "uq_trade_plan_account_symbol_version" in names


def test_optional_financials_missing_reduces_research_completeness(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()["decision_package"]
    assert package["research_decision"]["research_completeness"] < 100


def test_strategy_can_promote_financials_to_required(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
            "required_research_capabilities": ["financials"],
        },
    ).json()["decision_package"]
    assert "financials" in package["required_capabilities"]
    assert package["quality_status"] == "MISSING"
    assert package["freeze_allowed"] is False


def test_stale_required_evidence_blocks_freeze(client, session, monkeypatch):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    announcement_scan = session.query(CompanyResearchRefresh).filter_by(
        section="announcements"
    ).one()
    announcement_scan.checked_at = datetime.now() - timedelta(days=2)
    announcement_scan.quality_status = "STALE"
    session.commit()
    patch_benchmarks(monkeypatch)
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": False,
        },
    ).json()["decision_package"]
    assert package["quality_status"] == "STALE"
    assert package["freeze_allowed"] is False


def test_stale_optional_evidence_is_reported_but_not_blocking(
    client, session, monkeypatch
):
    account = create_account(client, assets="300000", cash="300000")
    seed_pattern(session)
    seed_profile(session)
    patch_benchmarks(monkeypatch)
    monkeypatch.setattr(
        "app.services.one_click_pipeline.run_ai_analysis",
        lambda db, request: {
            "status": "success",
            "id": None,
            "sources": [
                {
                    "source_id": "financial:test",
                    "symbol": "300502",
                    "category": "financial",
                    "source_name": "test",
                    "stale": True,
                    "content": {"revenue": 100},
                }
            ],
            "result": {"missing_data": []},
        },
    )
    package = client.post(
        "/api/v1/trade-plan-generator/analyze",
        json={
            "symbol": "300502",
            "position_mode": "空仓",
            "account_id": account["id"],
            "enable_ai": True,
        },
    ).json()["decision_package"]
    assert package["quality_status"] == "SINGLE_SOURCE"
    assert package["freeze_allowed"] is True
    assert "financials" in package["research_decision"]["missing_optional_evidence"]
