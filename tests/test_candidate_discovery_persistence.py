from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import Settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.market_subjects import industry_capital_flow_subject
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.discovery.data import persist_discovery_result, resolve_discovery_cache
from app.models import DataQualityRecord, IndustryCapitalFlowSnapshot, MarketEventPoolSnapshot
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
        return []

    def broken_limit_pool(self, day):
        return []


def _router(session, client=None):
    settings = Settings(
        astock_data_enabled=True,
        astock_data_timeout_seconds=1,
        astock_data_max_retries=1,
        astock_data_min_interval_seconds=0,
    )
    registry = ProviderRegistry()
    registry.register(
        AStockDiscoveryProvider(
            settings,
            client=client or DiscoveryClient(),
            now_fn=lambda: NOW,
        )
    )
    return DataHubRouter(session, registry, now_fn=lambda: NOW)


def test_capital_flow_persistence_binds_exact_quality_record(session):
    router = _router(session)
    result = router.get_industry_capital_flow(DAY)
    assert persist_discovery_result(session, router, result) == 1
    row = session.scalar(select(IndustryCapitalFlowSnapshot))
    assert row.quality_record_id == result.quality_record_id
    assert row.amount_unit == "CNY"
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True


def test_empty_pool_is_persisted_as_authoritative_zero_rows(session):
    router = _router(session)
    result = router.get_broken_limit_pool(DAY)
    assert persist_discovery_result(session, router, result) == 0
    assert session.scalars(select(MarketEventPoolSnapshot)).all() == []
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert record.row_count == 0
    assert record.persisted is True


def test_payload_tampering_is_rejected_before_delete_and_preserves_old_cache(session):
    first_router = _router(session)
    first = first_router.get_industry_capital_flow(DAY)
    persist_discovery_result(session, first_router, first)
    session.commit()
    old = session.scalar(select(IndustryCapitalFlowSnapshot))
    old_quality_id = old.quality_record_id
    old_amount = old.amount

    second_router = _router(session)
    second = second_router.get_industry_capital_flow(DAY)
    second.value[0] = second.value[0].__class__(
        **{**second.value[0].__dict__, "amount": second.value[0].amount * 2}
    )
    with pytest.raises(ProviderUnavailableError, match="payload digest"):
        persist_discovery_result(session, second_router, second)

    current = session.scalar(select(IndustryCapitalFlowSnapshot))
    assert current.quality_record_id == old_quality_id
    assert current.amount == old_amount
    assert session.get(DataQualityRecord, second.quality_record_id).persisted is False


def test_resolved_discovery_cache_uses_effective_quality(session):
    router = _router(session)
    result = router.get_industry_capital_flow(DAY)
    persist_discovery_result(session, router, result)
    session.commit()
    selected = resolve_discovery_cache(
        session,
        capability="market.industry.capital_flow",
        subject=industry_capital_flow_subject(),
        evaluated_at=NOW,
    )
    assert selected.executable is True
    assert selected.quality_record_id == result.quality_record_id
    assert len(selected.rows) == 1
