from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.models import DataQualityRecord
from app.providers.astock_discovery_provider import AStockDiscoveryProvider


NOW = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 7, 24)


class DiscoveryClient:
    def industry_capital_flow(self, day):
        return [
            {
                "industry_key": "BK0001",
                "industry_name": "通信设备",
                "trade_date": day.isoformat(),
                "net_inflow_1d": "100000000",
                "net_inflow_5d": "300000000",
                "net_inflow_10d": "500000000",
                "amount": "2000000000",
            }
        ]

    def limit_up_pool(self, day):
        return [
            {
                "symbol": "300001",
                "name": "测试股份",
                "trade_date": day.isoformat(),
                "first_event_at": "09:45:00",
                "last_event_at": "14:30:00",
                "sealed_amount": "80000000",
                "turnover_rate": "6.5",
                "consecutive_days": 2,
                "industry_name": "通信设备",
                "reason_summary": "公开事件数据",
            }
        ]

    def broken_limit_pool(self, day):
        return []


def _provider(client=None, *, enabled=True):
    settings = Settings(
        astock_data_enabled=enabled,
        astock_data_timeout_seconds=1,
        astock_data_max_retries=1,
        astock_data_min_interval_seconds=0,
        astock_data_cache_ttl_seconds=60,
    )
    return AStockDiscoveryProvider(
        settings,
        client=client or DiscoveryClient(),
        now_fn=lambda: NOW,
    )


def test_astock_provider_is_disabled_by_default_and_health_is_safe():
    provider = AStockDiscoveryProvider(Settings(), client=DiscoveryClient())
    assert provider.metadata.enabled is False
    assert provider.health_check()["status"] == "disabled"


def test_capital_flow_contract_has_fixed_cny_units():
    row = _provider().get_industry_capital_flow(DAY)[0]
    assert row.industry_key == "BK0001"
    assert row.net_inflow_5d == Decimal("300000000")
    assert row.amount_unit == "CNY"
    assert row.observed_at.tzinfo is not None
    assert row.fetched_at == NOW


def test_limit_pool_contract_has_decimal_percent_turnover():
    row = _provider().get_limit_up_pool(DAY)[0]
    assert row.event_type == "LIMIT_UP"
    assert row.turnover_rate == Decimal("6.5")
    assert row.turnover_rate_unit == "percent"
    assert row.sealed_amount_unit == "CNY"


def test_real_akshare_pool_shape_allows_implicit_day_and_integer_event_time():
    client = DiscoveryClient()
    client.limit_up_pool = lambda day: [
        {
            "代码": "300001",
            "名称": "测试股份",
            "首次封板时间": 94500,
            "最后封板时间": 143000,
            "封板资金": "80000000",
            "换手率": "6.5",
            "连板数": 2,
            "所属行业": "通信设备",
        }
    ]
    row = _provider(client).get_limit_up_pool(DAY)[0]
    assert row.trade_date == DAY
    assert row.first_event_at.hour == 9
    assert row.first_event_at.minute == 45
    assert row.last_event_at.hour == 14
    assert row.last_event_at.minute == 30


def test_empty_broken_pool_is_valid_not_provider_failure():
    assert _provider().get_broken_limit_pool(DAY) == []


@pytest.mark.parametrize("method", ["industry_capital_flow", "limit_up_pool"])
def test_html_error_page_is_rejected(method):
    client = DiscoveryClient()
    setattr(client, method, lambda day: "<html>rate limited</html>")
    provider = _provider(client)
    operation = (
        provider.get_industry_capital_flow
        if method == "industry_capital_flow"
        else provider.get_limit_up_pool
    )
    with pytest.raises(ProviderUnavailableError, match="HTML"):
        operation(DAY)


def test_provider_schema_change_is_not_filled_with_zero():
    client = DiscoveryClient()
    client.industry_capital_flow = lambda day: [{"industry_name": "通信设备"}]
    with pytest.raises(ProviderUnavailableError, match="missing fields"):
        _provider(client).get_industry_capital_flow(DAY)


def test_router_records_new_capability_as_single_source(session):
    registry = ProviderRegistry()
    registry.register(_provider())
    result = DataHubRouter(session, registry, now_fn=lambda: NOW).get_industry_capital_flow(DAY)
    assert result.quality_status.value == "SINGLE_SOURCE"
    assert result.subject.subject_type == "market"
    assert result.subject.subject_id == "CN-A"
    assert result.quality_record_id is not None
    assert result.value[0].industry_name == "通信设备"


def test_router_accepts_empty_event_pool_as_observed_single_source(session):
    registry = ProviderRegistry()
    registry.register(_provider())
    result = DataHubRouter(session, registry, now_fn=lambda: NOW).get_broken_limit_pool(DAY)
    assert result.quality_status.value == "SINGLE_SOURCE"
    assert result.value == []
    assert result.observed_at.date() == NOW.date()
    assert result.observed_at.hour == 15
    assert result.fetched_at == NOW


def test_provider_ttl_cache_is_visible_in_router_lineage(session):
    registry = ProviderRegistry()
    registry.register(_provider())
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    first = router.get_industry_capital_flow(DAY)
    second = router.get_industry_capital_flow(DAY)
    assert first.cache_used is False
    assert second.cache_used is True
    assert session.get(DataQualityRecord, second.quality_record_id).cache_used is True
