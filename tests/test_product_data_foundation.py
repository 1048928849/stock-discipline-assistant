from datetime import date, datetime
from decimal import Decimal

import pytest

from app.data_hub.contracts import (
    IndustryConstituent,
    IndustryDaily,
    IntradayBar,
    MarketAmountDaily,
    MarketBreadthDaily,
    ProviderMetadata,
    ProviderUnavailableError,
    TurnoverDaily,
)
from app.data_hub.market_subjects import (
    company_concepts_subject,
    company_industry_chain_subject,
    industry_constituents_subject,
    market_amount_subject,
    market_breadth_subject,
    stock_intraday_subject,
    stock_turnover_subject,
)
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.domain.quality import DataQualityStatus
from app.models import (
    CompanyChainPosition,
    CompanyConcept,
    IndustryConstituentSnapshot,
    IndustryMarketSnapshot,
    MarketAmountSnapshot,
    MarketBreadthSnapshot,
    MarketIntradayBar,
    MarketTurnoverSnapshot,
)
from app.providers.akshare_provider import AKShareProvider
from app.services.product_data import persist_product_result, resolve_product_cache


NOW = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)


def _bar(close: str = "10.50", *, completed: bool = True) -> IntradayBar:
    return IntradayBar(
        symbol="300502",
        trade_date=date(2026, 7, 24),
        bar_start=datetime(2026, 7, 24, 14, 0, tzinfo=SHANGHAI_TZ),
        bar_end=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
        open=Decimal("10.10"),
        high=Decimal("10.70"),
        low=Decimal("10.00"),
        close=Decimal(close),
        volume=Decimal("100000"),
        amount=Decimal("1040000"),
        turnover_rate=Decimal("1.25"),
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
        source="fixture",
        fetched_at=NOW,
        completed=completed,
    )


class ProductProvider:
    def __init__(self, provider_id: str = "product-fixture", close: str = "10.50"):
        self.close = close
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=("market.intraday.60m",),
            priority=1,
        )

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

    def get_intraday_60m(self, symbol, start, end):
        del symbol, start, end
        return [_bar(self.close)]


class FullProductProvider(ProductProvider):
    def __init__(self):
        super().__init__()
        self.metadata = ProviderMetadata(
            provider_id="full-product-fixture",
            supported_capabilities=(
                "market.intraday.60m",
                "market.turnover.daily",
                "market.breadth.daily",
                "market.amount.daily",
                "market.industry.daily",
                "market.industry.constituents",
                "company.concepts",
                "company.industry_chain",
            ),
            priority=1,
        )

    def get_turnover_daily(self, symbol, start, end):
        del start, end
        return [
            TurnoverDaily(
                symbol=symbol,
                trade_date=date(2026, 7, 24),
                turnover_rate=Decimal("2.5"),
                amount=Decimal("2000000"),
                observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
                source="fixture",
                fetched_at=NOW,
            )
        ]

    def get_market_breadth(self, day):
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
                observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
                source="fixture",
                fetched_at=NOW,
            )
        ]

    def get_market_amount(self, day):
        return [
            MarketAmountDaily(
                trade_date=day,
                total_amount=Decimal("1200000000000"),
                observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
                source="fixture",
                fetched_at=NOW,
            )
        ]

    def get_industry_daily(self, industry, start, end):
        del start, end
        return [
            IndustryDaily(
                industry=industry,
                trade_date=date(2026, 7, 24),
                change_pct=Decimal("2.2"),
                amount=Decimal("50000000000"),
                amount_share=Decimal("0.05"),
                advance_ratio=Decimal("0.75"),
                limit_up_count=4,
                leader_strength=Decimal("9.9"),
                new_high_ratio=Decimal("0.2"),
                observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
                source="fixture",
                fetched_at=NOW,
            )
        ]

    def get_industry_constituents(self, industry):
        return [
            IndustryConstituent(
                industry=industry,
                symbol="300502",
                name="fixture company",
                weight=Decimal("1.5"),
                observed_at=NOW,
                source="fixture",
                fetched_at=NOW,
            )
        ]

    def company_concepts(self, symbol):
        return [
            {
                "concept": "AI hardware",
                "relevance": "CORE_BUSINESS",
                "evidence_summary": "Annual report product disclosure",
                "source": "fixture filing",
                "observed_at": NOW,
                "fetched_at": NOW,
            }
        ]

    def company_industry_chain(self, symbol):
        return [
            {
                "chain": "AI infrastructure",
                "node": "core equipment",
                "stage": "CORE_EQUIPMENT",
                "relevance": "CORE_BUSINESS",
                "primary_products": ["server"],
                "revenue_relevance": "unknown",
                "source": "fixture filing",
                "evidence_summary": "Annual report product disclosure",
                "observed_at": NOW,
                "fetched_at": NOW,
            }
        ]


def _router(session, *providers) -> DataHubRouter:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry, now_fn=lambda: NOW)


def test_product_subjects_have_stable_semantic_scopes():
    assert stock_intraday_subject("300502").semantic_key == "60m/qfq/CNY/share"
    assert stock_turnover_subject("300502").semantic_key == "daily/ratio"
    assert market_breadth_subject().stable_key == ("market", "CN-A", "daily/all-a")
    assert market_amount_subject().stable_key == ("market", "CN-A", "daily/CNY")
    assert industry_constituents_subject(" 电子 ").semantic_key == "constituents/current"
    assert company_concepts_subject("300502").semantic_key == "concepts/current"
    assert company_industry_chain_subject("300502").semantic_key == (
        "industry-chain/current"
    )


def test_incomplete_intraday_bar_is_missing_and_not_persistable(session):
    provider = ProductProvider()
    provider.get_intraday_60m = lambda symbol, start, end: [_bar(completed=False)]
    router = _router(session, provider)
    result = router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    assert result.quality_status == DataQualityStatus.MISSING
    with pytest.raises(ProviderUnavailableError):
        persist_product_result(session, router, result)


def test_intraday_refresh_is_idempotent_and_exactly_bound(session):
    router = _router(session, ProductProvider())
    first = router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    assert persist_product_result(session, router, first) == 1
    session.commit()
    first_record = first.quality_record_id

    second = router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    assert persist_product_result(session, router, second) == 1
    session.commit()
    stored = session.query(MarketIntradayBar).one()
    assert stored.close == Decimal("10.5000")
    assert stored.quality_record_id == second.quality_record_id
    assert stored.quality_record_id != first_record


def test_conflicted_refresh_preserves_existing_intraday_cache(session):
    trusted_router = _router(session, ProductProvider())
    trusted = trusted_router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    persist_product_result(session, trusted_router, trusted)
    session.commit()
    original_id = session.query(MarketIntradayBar).one().quality_record_id

    conflicted_router = _router(
        session,
        ProductProvider("source-a", "10.50"),
        ProductProvider("source-b", "20.00"),
    )
    conflicted = conflicted_router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    assert conflicted.quality_status == DataQualityStatus.CONFLICTED
    with pytest.raises(ProviderUnavailableError):
        persist_product_result(session, conflicted_router, conflicted)
    session.commit()
    stored = session.query(MarketIntradayBar).one()
    assert stored.close == Decimal("10.5000")
    assert stored.quality_record_id == original_id


def test_product_persistence_rejects_result_from_another_router(session):
    first_router = _router(session, ProductProvider("source-a"))
    result = first_router.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    with pytest.raises(ProviderUnavailableError, match="exact Router call lineage"):
        persist_product_result(
            session,
            _router(session, ProductProvider("source-a")),
            result,
        )


def test_all_product_capabilities_persist_with_exact_lineage(session):
    router = _router(session, FullProductProvider())
    calls = [
        router.get_turnover_daily("300502", date(2026, 7, 1), date(2026, 7, 24)),
        router.get_market_breadth(date(2026, 7, 24)),
        router.get_market_amount(date(2026, 7, 24)),
        router.get_industry_daily("electronics", date(2026, 7, 1), date(2026, 7, 24)),
        router.get_industry_constituents("electronics"),
        router.company_concepts("300502"),
        router.company_industry_chain("300502"),
    ]
    for result in calls:
        assert result.quality_status == DataQualityStatus.SINGLE_SOURCE
        assert persist_product_result(session, router, result) == 1
    session.commit()
    assert session.query(MarketTurnoverSnapshot).count() == 1
    assert session.query(MarketBreadthSnapshot).count() == 1
    assert session.query(MarketAmountSnapshot).count() == 1
    assert session.query(IndustryMarketSnapshot).count() == 1
    assert session.query(IndustryConstituentSnapshot).count() == 1
    assert session.query(CompanyConcept).count() == 1
    assert session.query(CompanyChainPosition).count() == 1
    assert all(result.quality_record_id for result in calls)
    concept_cache = resolve_product_cache(
        session,
        capability="company.concepts",
        subject=company_concepts_subject("300502"),
        evaluated_at=NOW,
    )
    assert concept_cache.executable is True
    assert concept_cache.quality_record_id == calls[-2].quality_record_id


def test_company_mapping_refresh_is_idempotent(session):
    router = _router(session, FullProductProvider())
    first = router.company_concepts("300502")
    persist_product_result(session, router, first)
    session.commit()
    second = router.company_concepts("300502")
    persist_product_result(session, router, second)
    session.commit()
    assert session.query(CompanyConcept).count() == 1
    assert session.query(CompanyConcept).one().quality_record_id == (
        second.quality_record_id
    )


def test_product_cache_selector_recomputes_freshness(session):
    router = _router(session, FullProductProvider())
    result = router.get_turnover_daily(
        "300502", date(2026, 7, 1), date(2026, 7, 24)
    )
    persist_product_result(session, router, result)
    session.commit()
    fresh = resolve_product_cache(
        session,
        capability="market.turnover.daily",
        subject=stock_turnover_subject("300502"),
        evaluated_at=NOW,
    )
    assert fresh.executable is True
    assert fresh.quality_record_id == result.quality_record_id
    stale = resolve_product_cache(
        session,
        capability="market.turnover.daily",
        subject=stock_turnover_subject("300502"),
        evaluated_at=datetime(2026, 7, 27, 15, 30, tzinfo=SHANGHAI_TZ),
    )
    assert stale.effective_quality.effective_quality == DataQualityStatus.STALE
    assert stale.executable is False


def test_akshare_intraday_provider_uses_real_endpoint_and_drops_incomplete_bar():
    pd = pytest.importorskip("pandas")

    class FakeAkshare:
        def stock_zh_a_hist_min_em(self, **kwargs):
            assert kwargs["period"] == "60"
            assert kwargs["adjust"] == "qfq"
            return pd.DataFrame(
                [
                    {
                        "datetime": "2026-07-24 15:00:00",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "volume": 100,
                        "amount": 1000,
                        "turnover": 1.2,
                    },
                    {
                        "datetime": "2026-07-24 16:00:00",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.6,
                        "volume": 100,
                        "amount": 1000,
                        "turnover": 1.2,
                    },
                ]
            )

    provider = AKShareProvider(now_fn=lambda: NOW)
    provider._ak = lambda: FakeAkshare()
    rows = provider.get_intraday_60m(
        "300502",
        datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
        NOW,
    )
    assert [row.bar_end.hour for row in rows] == [15]
    assert rows[0].completed is True


def test_akshare_declares_all_product_capabilities():
    supported = set(AKShareProvider(now_fn=lambda: NOW).metadata.supported_capabilities)
    assert {
        "market.intraday.60m",
        "market.turnover.daily",
        "market.breadth.daily",
        "market.amount.daily",
        "market.industry.daily",
        "market.industry.constituents",
        "company.concepts",
        "company.industry_chain",
    } <= supported
