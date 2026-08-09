from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.data_hub.market_subjects import (
    industry_constituents_subject,
    industry_membership_subject,
)
from app.data_hub.trading_calendar import to_market_storage_naive, to_utc_storage_naive
from app.history.service import HistoryPlanningBlocked
from app.models import (
    DataQualityRecord,
    IndustryConstituentSnapshot,
    IndustryTaxonomyBinding,
)
from app.selected_stock.industry_bootstrap import IndustryEvidenceBootstrap


DAY = date(2026, 7, 31)
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _quality(session, capability, subject, *, provider="akshare"):
    market = capability.startswith("market.")
    stored = to_market_storage_naive(NOW) if market else to_utc_storage_naive(NOW)
    record = DataQualityRecord(
        symbol=subject.subject_id,
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=stored,
        fetched_at=stored,
        provider_id=provider,
        provider_observations=[],
        normalized_digest="a" * 64,
        conflict_fields=[],
        row_count=2,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    return record


def _binding_and_members(session, *, include_target=True, provider="akshare"):
    industry = "通信设备"
    subject = industry_constituents_subject(industry)
    membership = _quality(
        session,
        "industry.membership.native",
        industry_membership_subject("300308"),
        provider=provider,
    )
    binding = IndustryTaxonomyBinding(
        symbol="300308",
        classification_system="EASTMONEY_INDUSTRY",
        provider_id=provider,
        provider_industry_id=subject.subject_id,
        provider_industry_code=None,
        provider_industry_name=industry,
        level="PROVIDER_NATIVE",
        effective_date=DAY,
        observed_at=to_utc_storage_naive(NOW),
        fetched_at=to_utc_storage_naive(NOW),
        source_reference="fixture",
        response_digest="a" * 64,
        membership_evidence="exact constituent inclusion",
        quality_record_id=membership.id,
    )
    session.add(binding)
    constituent_quality = _quality(
        session, "market.industry.constituents", subject, provider=provider
    )
    symbols = ["300308", "300502"] if include_target else ["300502"]
    for symbol in symbols:
        session.add(
            IndustryConstituentSnapshot(
                industry_key=subject.subject_id,
                industry_name=industry,
                symbol=symbol,
                name=symbol,
                weight=None,
                snapshot_date=DAY,
                observed_at=to_market_storage_naive(NOW),
                source="fixture",
                fetched_at=to_market_storage_naive(NOW),
                quality_record_id=constituent_quality.id,
            )
        )
    session.flush()
    return binding


def test_industry_bootstrap_plan_uses_exact_native_universe(session):
    binding = _binding_and_members(session)
    bootstrap = IndustryEvidenceBootstrap(session)
    bootstrap._binding = binding
    plan = bootstrap._plan(trade_date=DAY, now=NOW)
    assert plan.required_stock_symbols == ("300308", "300502")
    assert plan.minimum_rows == 120
    assert plan.required_capabilities == ("market.daily.qfq", "market.turnover.daily")


def test_industry_bootstrap_blocks_membership_conflict(session):
    binding = _binding_and_members(session, include_target=False)
    bootstrap = IndustryEvidenceBootstrap(session)
    bootstrap._binding = binding
    with pytest.raises(HistoryPlanningBlocked, match="INDUSTRY_MEMBERSHIP_CONFLICTED"):
        bootstrap._plan(trade_date=DAY, now=NOW)
