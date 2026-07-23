from datetime import date, timedelta

from app.config import Settings
from app.models import (
    CompanyFinancialPeriod,
    DataProviderCallLog,
)
from app.providers.base import ProviderMetadata
from app.providers.external_http_provider import (
    ConfiguredNewsApiProvider,
    ProfessionalMarketApiProvider,
)
from app.providers.registry import ProviderRegistry
from app.providers.tushare_provider import TushareProvider
from app.services.company_research import refresh_company_research_if_needed
from app.services.data_sources import UnifiedDataService


def _statements():
    balance, income, cash = [], [], []
    for index in range(12):
        year = 2023 + index // 4
        month = (3, 6, 9, 12)[index % 4]
        day = 31 if month in (3, 12) else 30
        report = date(year, month, day).strftime("%Y%m%d")
        quarter = index % 4 + 1
        balance.append(
            {
                "报告日": report,
                "资产总计": 1000 + index * 10,
                "负债合计": 400 + index * 4,
                "归属于母公司股东权益合计": 600 + index * 6,
                "应收账款": 50 + index,
                "存货": 40 + index,
            }
        )
        income.append(
            {
                "报告日": report,
                "营业收入": quarter * (100 + index),
                "营业成本": quarter * (60 + index),
                "净利润": quarter * (15 + index),
                "归属于母公司所有者的净利润": quarter * (15 + index),
                "研发费用": quarter * 5,
            }
        )
        cash.append(
            {"报告日": report, "经营活动产生的现金流量净额": quarter * (14 + index)}
        )
    return {"balance": balance, "income": income, "cash_flow": cash}


class FreeResearchProvider:
    provider_id = "free_test"

    def __init__(self, fail=False):
        self.fail = fail
        self.metadata = ProviderMetadata(
            provider_id=self.provider_id,
            supported_capabilities=(
                "fundamental.profile",
                "fundamental.statements",
                "fundamental.valuation",
                "announcement.catalog",
            ),
            priority=1,
            health_status="healthy",
        )

    @property
    def configured(self):
        return True

    def health_check(self, probe=False):
        return {"status": "healthy", "message": "test"}

    def credential_status(self):
        return {"configured": True, "required_credentials": []}

    def _check(self):
        if self.fail:
            raise RuntimeError("测试Provider故障")

    def company_profile(self, symbol):
        self._check()
        return {
            "A股简称": "真实测试公司",
            "细分行业": "通信设备",
            "所属市场": "创业板",
            "主营业务": "光通信设备",
        }

    def financial_statements(self, symbol):
        self._check()
        return _statements()

    def company_announcements(self, symbol, start, end):
        self._check()
        return [
            {
                "公告标题": "关于日常经营事项的公告",
                "公告日期": date.today().isoformat(),
                "网址": "https://example.test/official-announcement",
                "目录来源": "巨潮资讯",
            }
        ]

    def valuation_history(self, symbol):
        self._check()
        start = date.today() - timedelta(days=90)
        values = [
            {"date": (start + timedelta(days=index)).isoformat(), "value": 20 + index / 10}
            for index in range(90)
        ]
        return {"pe_ttm": values, "pb": values, "market_cap": values}

    def valuation_comparison(self, symbol):
        self._check()
        return [{"代码": symbol, "市销率-TTM": 3}]


def _service(session, provider):
    registry = ProviderRegistry()
    registry.register(provider)
    return UnifiedDataService(session, registry)


def test_tushare_unconfigured_is_registered_and_safely_skipped():
    provider = TushareProvider(Settings(tushare_enabled=True, tushare_token=""))
    assert provider.configured is False
    assert provider.health_check()["status"] == "not_configured"
    registry = ProviderRegistry()
    registry.register(provider)
    assert registry.providers_for("market.daily")[0].provider_id == "tushare"


def test_optional_paid_provider_credentials_are_detected_without_network_calls():
    settings = Settings(
        professional_market_api_enabled=True,
        news_api_enabled=True,
    )
    market = ProfessionalMarketApiProvider(settings)
    news = ConfiguredNewsApiProvider(settings)
    assert market.configured is False
    assert news.configured is False
    assert market.health_check()["status"] == "not_configured"
    assert news.health_check()["status"] == "not_configured"


def test_auto_refresh_twelve_quarters_and_cache_fallback(session):
    first = refresh_company_research_if_needed(
        session,
        "300502",
        force=True,
        data_service=_service(session, FreeResearchProvider()),
    )
    assert first["missing_data"] == []
    rows = session.query(CompanyFinancialPeriod).filter_by(symbol="300502").all()
    assert len(rows) == 12
    assert any(item["status"] == "success" for item in first["sections"].values())
    failed = refresh_company_research_if_needed(
        session,
        "300502",
        force=True,
        data_service=_service(session, FreeResearchProvider(fail=True)),
    )
    assert failed["missing_data"] == []
    assert failed["sections"]["financials"]["status"] == "cache_fallback"
    logs = session.query(DataProviderCallLog).all()
    assert any(item.status == "failed" for item in logs)
    assert any(item.status == "cache_fallback" for item in logs)


def test_no_provider_and_no_cache_is_explicit_missing_data(session):
    result = refresh_company_research_if_needed(
        session,
        "600000",
        force=True,
        data_service=_service(session, FreeResearchProvider(fail=True)),
    )
    assert set(result["missing_data"]) == {
        "profile",
        "financials",
        "announcements",
        "valuation",
    }
    assert result["status"] == "partial"
