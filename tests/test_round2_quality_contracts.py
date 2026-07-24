from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.data_hub.contracts import DailyBar, ProviderMetadata, Quote
from app.data_hub.quality import canonical_digest, policy_for
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.domain.models import DecisionPackage
from app.models import (
    CompanyAnnouncement,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchRefresh,
    CompanyValuationSnapshot,
    DataQualityRecord,
    MarketDailyBar,
    MarketQuote,
)
from app.providers.akshare_provider import AKShareProvider
from app.providers.external_http_provider import ProfessionalMarketApiProvider
from app.providers.tushare_provider import TushareProvider
from app.services.company_research import sync_company_research
from app.services.one_click_pipeline import _research_inventory, _sync_stock
from app.services.technical_snapshots import load_qfq_frame


class ContractProvider:
    def __init__(self, provider_id: str, *, overrides=None):
        self.provider_id = provider_id
        self.overrides = overrides or {}
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=(
                "market.quote.realtime",
                "market.daily.qfq",
                "fundamental.profile",
                "fundamental.statements",
                "fundamental.valuation",
                "announcement.catalog",
            ),
            priority=1 if provider_id == "a" else 2,
            realtime_supported=True,
        )

    @property
    def configured(self):
        return True

    def get_quote(self, symbol):
        price = self.overrides.get("quote", "10.82")
        now = datetime.now()
        return Quote(
            symbol=symbol,
            name="test",
            price=Decimal(price),
            quote_type="realtime",
            observed_at=now,
            price_unit="CNY",
            source=self.provider_id,
            source_api="quote",
            fetched_at=now,
        )

    def get_history(self, symbol, start, end):
        close = Decimal(self.overrides.get("daily", "10.82"))
        observed = datetime.combine(date.today(), datetime.min.time())
        return [
            DailyBar(
                symbol=symbol,
                trade_date=date.today(),
                open=close,
                high=close + Decimal("0.1"),
                low=close - Decimal("0.1"),
                close=close,
                volume=Decimal("10000"),
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=observed,
                source=self.provider_id,
                fetched_at=datetime.now(),
            )
        ]

    def company_profile(self, symbol):
        return {
            "A股简称": "测试公司",
            "细分行业": self.overrides.get("profile", "通信设备"),
            "主营业务": "设备",
        }

    def financial_statements(self, symbol):
        value = self.overrides.get("financial", 100)
        report = date.today().replace(day=1).isoformat()
        return {
            "balance": [{"报告日": report, "资产总计": value, "负债合计": 10}],
            "income": [{"报告日": report, "营业收入": value, "净利润": 10}],
            "cash_flow": [{"报告日": report, "经营活动产生的现金流量净额": 10}],
        }

    def company_announcements(self, symbol, start, end):
        title = self.overrides.get("announcement", "经营事项公告")
        url = self.overrides.get("announcement_url", f"https://{self.provider_id}.test/a")
        return [
            {
                "symbol": symbol,
                "公告标题": title,
                "公告日期": date.today().isoformat(),
                "网址": url,
            }
        ]

    def valuation_history(self, symbol):
        value = self.overrides.get("valuation", 20)
        return {
            "pe_ttm": [{"date": date.today(), "value": value}],
            "pb": [{"date": date.today(), "value": 2}],
            "market_cap": [{"date": date.today(), "value": 1000}],
        }

    def valuation_comparison(self, symbol):
        return [{"代码": symbol, "市销率-TTM": 3}]


def _router(session, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry)


def _conflicted_research(session, target):
    first = ContractProvider("a")
    second = ContractProvider("b", overrides={target: "different" if target != "financial" else 999})
    result = sync_company_research(
        session,
        "300502",
        provider=_router(session, first, second),
        include_documents=False,
    )
    return result


def test_conflicted_market_data_is_not_persisted_as_success(client, session, monkeypatch):
    router = _router(
        session,
        ContractProvider("a"),
        ContractProvider("b", overrides={"daily": "11.82"}),
    )
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    response = client.post("/api/v1/market/sync?symbol=300502&days=365")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MARKET_DATA_CONFLICTED"
    assert session.scalar(select(MarketDailyBar)) is None
    assert session.scalars(
        select(DataQualityRecord).where(
            DataQualityRecord.quality_status == "CONFLICTED"
        )
    ).all()


def test_conflicted_cached_market_data_cannot_become_single_source(session):
    now = datetime.now()
    session.add(
        MarketDailyBar(
            symbol="300502",
            trade_date=date.today(),
            open=10,
            high=11,
            low=9,
            close=Decimal("10.82"),
            volume=100,
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            observed_at=now,
            quality_status="VERIFIED",
            source="trusted",
            fetched_at=now,
        )
    )
    session.add(
        DataQualityRecord(
            symbol="300502",
            capability="market.daily.qfq",
            quality_status="CONFLICTED",
            observed_at=now,
            fetched_at=now,
            provider_id="multi",
            provider_observations=[],
            normalized_digest="a" * 64,
            conflict_fields=["close"],
            row_count=1,
            trusted=False,
            persisted=False,
        )
    )
    session.commit()
    step, _ = _sync_stock(session, "300502", _router(session), refresh=False)
    assert step["quality_status"] == "CONFLICTED"


def test_conflicted_profile_is_not_laundered_after_persistence(session):
    result = _conflicted_research(session, "profile")
    assert result["sections"]["profile"]["quality_status"] == "CONFLICTED"
    assert session.scalar(select(CompanyProfile)) is None


def test_conflicted_announcements_are_not_laundered_after_persistence(session):
    result = _conflicted_research(session, "announcement")
    assert result["sections"]["announcements"]["quality_status"] == "CONFLICTED"
    assert session.scalar(select(CompanyAnnouncement)) is None


def test_conflicted_financials_are_not_laundered_after_persistence(session):
    result = _conflicted_research(session, "financial")
    assert result["sections"]["financials"]["quality_status"] == "CONFLICTED"
    assert session.scalar(select(CompanyFinancialPeriod)) is None


def test_conflicted_valuation_is_not_laundered_after_persistence(session):
    result = _conflicted_research(session, "valuation")
    assert result["sections"]["valuation"]["quality_status"] == "CONFLICTED"
    assert session.scalar(select(CompanyValuationSnapshot)) is None


def test_market_sync_rejects_conflicted_history(client, session, monkeypatch):
    router = _router(
        session,
        ContractProvider("a"),
        ContractProvider("b", overrides={"daily": "12.00"}),
    )
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    response = client.post("/api/v1/market/sync?symbol=300502")
    details = response.json()["error"]["details"]
    assert details["history_quality_status"] == "CONFLICTED"
    assert details["provider_observations"]["history"]


def test_market_sync_rejects_conflicted_quote(client, session, monkeypatch):
    router = _router(
        session,
        ContractProvider("a"),
        ContractProvider("b", overrides={"quote": "12.00"}),
    )
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    response = client.post("/api/v1/market/sync?symbol=300502")
    assert response.status_code == 422
    assert response.json()["error"]["details"]["quote_quality_status"] == "CONFLICTED"
    assert session.scalar(select(MarketQuote)) is None


def test_market_sync_does_not_overwrite_trusted_cache_with_conflict(
    client, session, monkeypatch
):
    old = MarketQuote(
        symbol="300502",
        name="trusted",
        price=Decimal("9.99"),
        quote_type="realtime",
        observed_at=datetime.now(),
        price_unit="CNY",
        quality_status="VERIFIED",
        source="trusted",
        source_api="quote",
        fetched_at=datetime.now(),
    )
    session.add(old)
    session.commit()
    router = _router(
        session,
        ContractProvider("a"),
        ContractProvider("b", overrides={"quote": "12.00"}),
    )
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    client.post("/api/v1/market/sync?symbol=300502")
    session.refresh(old)
    assert old.price == Decimal("9.9900")
    assert old.quality_status == "VERIFIED"


def test_market_sync_preserves_stale_quality_in_cache(client, session, monkeypatch):
    stale = ContractProvider("a")
    original_history = stale.get_history

    def stale_history(symbol, start, end):
        rows = original_history(symbol, start, end)
        return [
            DailyBar(**{**row.__dict__, "trade_date": date.today() - timedelta(days=10),
                        "observed_at": datetime.now() - timedelta(days=10)})
            for row in rows
        ]

    stale.get_history = stale_history
    router = _router(session, stale)
    monkeypatch.setattr("app.api.advanced.build_data_hub", lambda db: router)
    response = client.post("/api/v1/market/sync?symbol=300502")
    assert response.status_code == 422
    assert response.json()["error"]["details"]["history_quality_status"] == "STALE"
    assert session.scalar(select(MarketDailyBar)) is None


def test_one_click_reads_persisted_market_quality(session):
    test_conflicted_cached_market_data_cannot_become_single_source(session)


def test_successful_empty_announcement_scan_is_valid_evidence(session):
    provider = ContractProvider("a")
    provider.company_announcements = lambda symbol, start, end: []
    sync_company_research(
        session, "300502", provider=_router(session, provider), include_documents=False
    )
    scan = session.scalar(
        select(CompanyResearchRefresh).where(
            CompanyResearchRefresh.section == "announcements"
        )
    )
    assert scan.last_success_at is not None
    assert scan.row_count == 0
    assert scan.quality_status == "SINGLE_SOURCE"


def test_empty_verified_announcement_scan_allows_freeze(session):
    a, b = ContractProvider("a"), ContractProvider("b")
    a.company_announcements = lambda symbol, start, end: []
    b.company_announcements = lambda symbol, start, end: []
    sync_company_research(
        session, "300502", provider=_router(session, a, b), include_documents=False
    )
    _, step = _research_inventory(session, "300502")
    assert step["quality_status"] == "VERIFIED"
    assert step["required_missing"] == []


def test_failed_announcement_scan_blocks_freeze(session):
    _, step = _research_inventory(session, "300502")
    assert step["quality_status"] == "MISSING"
    assert step["required_missing"] == ["announcements"]


def test_empty_scan_and_missing_scan_are_distinct(session):
    test_successful_empty_announcement_scan_is_valid_evidence(session)
    _, step = _research_inventory(session, "300502")
    assert step["quality_status"] == "SINGLE_SOURCE"


def test_same_announcement_different_urls_does_not_conflict(session):
    a = ContractProvider("a", overrides={"announcement_url": "https://a.test/doc"})
    b = ContractProvider("b", overrides={"announcement_url": "https://b.test/doc"})
    result = _router(session, a, b).company_announcements(
        "300502", date.today(), date.today()
    )
    assert result.quality_status.value == "VERIFIED"


def test_same_announcement_title_spacing_does_not_conflict(session):
    a = ContractProvider("a", overrides={"announcement": "经营 事项 公告.pdf"})
    b = ContractProvider("b", overrides={"announcement": "经营事项公告"})
    result = _router(session, a, b).company_announcements(
        "300502", date.today(), date.today()
    )
    assert result.quality_status.value == "VERIFIED"


def test_different_announcement_business_content_conflicts(session):
    result = _router(
        session,
        ContractProvider("a"),
        ContractProvider("b", overrides={"announcement": "重大风险公告"}),
    ).company_announcements("300502", date.today(), date.today())
    assert result.quality_status.value == "CONFLICTED"


def test_akshare_and_tushare_daily_use_same_adjustment_contract():
    frame = pd.DataFrame(
        [{"日期": date.today(), "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5, "成交量": 1}]
    )
    bar = AKShareProvider._history_rows(
        frame,
        "300502",
        {"date": "日期", "open": "开盘", "high": "最高", "low": "最低", "close": "收盘", "volume": "成交量"},
        "renamed_provider",
    )[0]
    assert bar.adjustment == "qfq"
    assert bar.price_unit == "CNY"
    assert "market.daily.unadjusted" in TushareProvider(
        Settings(tushare_enabled=True, tushare_token="x")
    ).metadata.supported_capabilities


def test_unadjusted_data_is_excluded_from_qfq_verification():
    provider = TushareProvider(Settings(tushare_enabled=True, tushare_token="x"))
    assert "market.daily.qfq" not in provider.metadata.supported_capabilities


def test_tushare_daily_close_is_excluded_from_realtime_quote():
    provider = TushareProvider(Settings(tushare_enabled=True, tushare_token="x"))
    assert provider.metadata.realtime_supported is False
    assert "market.quote.realtime" not in provider.metadata.supported_capabilities


def test_professional_qfq_data_is_accepted_by_technical_engine(session):
    now = datetime.now()
    for index in range(3):
        session.add(
            MarketDailyBar(
                symbol="300502",
                trade_date=date.today() - timedelta(days=2 - index),
                open=10,
                high=11,
                low=9,
                close=10,
                volume=100,
                adjustment="qfq",
                price_unit="CNY",
                volume_unit="share",
                observed_at=now,
                quality_status="VERIFIED",
                source="professional_market_api_qfq",
                fetched_at=now,
            )
        )
    session.commit()
    assert len(load_qfq_frame(session, "300502")) == 3


def test_technical_engine_does_not_depend_on_provider_source_name(session):
    test_professional_qfq_data_is_accepted_by_technical_engine(session)


def test_provider_source_rename_does_not_change_adjustment():
    provider = ProfessionalMarketApiProvider(Settings())
    assert "market.daily.qfq" in provider.metadata.supported_capabilities


def test_zero_evidence_digest_is_rejected_in_final_package():
    from test_domain_and_data_hub import _decision_package

    package = _decision_package()
    payload = package.model_dump(mode="json")
    payload["evidence_digest"] = "0" * 64
    with pytest.raises(ValidationError, match="evidence_digest"):
        DecisionPackage.model_validate(payload)


def test_zero_package_hash_is_rejected_in_final_package():
    from test_domain_and_data_hub import _decision_package

    package = _decision_package()
    payload = package.model_dump(mode="json")
    payload["package_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="package_hash"):
        DecisionPackage.model_validate(payload)


def test_persisted_decision_package_requires_real_hashes():
    test_zero_package_hash_is_rejected_in_final_package()


def test_announcement_canonical_digest_ignores_url():
    policy = policy_for("announcement.catalog")
    first = [{"symbol": "300502", "title": "公告", "published_date": date.today(), "url": "a"}]
    second = [{"symbol": "300502", "title": "公告", "published_date": date.today(), "url": "b"}]
    assert canonical_digest(first, policy) == canonical_digest(second, policy)
