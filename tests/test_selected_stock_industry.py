from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.data_hub.market_subjects import (
    industry_constituents_subject,
    stock_daily_subject,
)
from app.data_hub.trading_calendar import get_trading_calendar, to_market_storage_naive
from app.models import (
    DataQualityRecord,
    IndustryConstituentSnapshot,
    MarketDailyBar,
    MarketTurnoverSnapshot,
)
from app.selected_stock.contracts import (
    ContextStatus,
    SourceLineage,
    StockRole,
    StockRoleEvidence,
)
from app.selected_stock.industry import (
    SelectedStockIndustryContextProvider,
    classify_role,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=SHANGHAI)
END = date(2026, 7, 31)


def _sessions(count: int) -> list[date]:
    calendar = get_trading_calendar()
    result = []
    day = END
    while len(result) < count:
        if calendar.is_session(day):
            result.append(day)
        day -= timedelta(days=1)
    return sorted(result)


def _quality(session, capability: str, subject, rows: int) -> DataQualityRecord:
    observed = to_market_storage_naive(get_trading_calendar().session_close_at(END))
    record = DataQualityRecord(
        symbol=subject.subject_id if subject.subject_type == "stock" else None,
        capability=capability,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        semantic_key=subject.semantic_key,
        quality_status="SINGLE_SOURCE",
        observed_at=observed,
        fetched_at=observed,
        provider_id="selected-industry-fixture",
        provider_observations=[],
        normalized_digest=(str(rows % 10) or "1") * 64,
        conflict_fields=[],
        row_count=rows,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    return record


def _seed_industry(session, *, ready_members: int = 5) -> tuple[list[dict], SourceLineage]:
    industry = "fixture-industry"
    symbols = ["300308", "000001", "000002", "600519", "920985"]
    constituent_subject = industry_constituents_subject(industry)
    constituent_quality = _quality(
        session, "market.industry.constituents", constituent_subject, len(symbols)
    )
    observed = to_market_storage_naive(get_trading_calendar().session_close_at(END))
    for index, symbol in enumerate(symbols):
        session.add(
            IndustryConstituentSnapshot(
                industry_key=constituent_subject.subject_id,
                industry_name=industry,
                symbol=symbol,
                name=f"member-{index}",
                weight=Decimal("0.08") if symbol == "300308" else Decimal("0.02"),
                change_pct=Decimal("1"),
                latest_price=Decimal("10"),
                high_52w=Decimal("12"),
                is_new_high=False,
                snapshot_date=END,
                observed_at=observed,
                source="fixture",
                fetched_at=observed,
                quality_record_id=constituent_quality.id,
            )
        )
    sessions = _sessions(130)
    for member_index, symbol in enumerate(symbols[:ready_members]):
        quality = _quality(
            session,
            "market.daily.qfq",
            stock_daily_subject(symbol, "qfq", "CNY", "share"),
            len(sessions),
        )
        for row_index, day in enumerate(sessions):
            slope = Decimal("0.08") if symbol == "300308" else Decimal("0.01")
            close = Decimal("10") + slope * Decimal(row_index) - Decimal(member_index) / 10
            session.add(
                MarketDailyBar(
                    symbol=symbol,
                    trade_date=day,
                    open=close - Decimal("0.02"),
                    high=close + Decimal("0.05"),
                    low=close - Decimal("0.05"),
                    close=close,
                    volume=Decimal("1000000"),
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=to_market_storage_naive(
                        get_trading_calendar().session_close_at(day)
                    ),
                    quality_status="SINGLE_SOURCE",
                    quality_record_id=quality.id,
                    source=f"fixture-{symbol}",
                    fetched_at=observed,
                )
            )
        turnover_quality = _quality(
            session,
            "market.turnover.daily",
            stock_daily_subject(symbol, "qfq", "CNY", "share"),
            1,
        )
        session.add(
            MarketTurnoverSnapshot(
                symbol=symbol,
                trade_date=END,
                turnover_rate=Decimal("2"),
                amount=Decimal("1000000000")
                if symbol == "300308"
                else Decimal("10000000") + Decimal(member_index),
                observed_at=observed,
                source="fixture",
                fetched_at=observed,
                quality_record_id=turnover_quality.id,
            )
        )
    session.commit()
    industry_rows = [
        {
            "trade_date": day,
            "open": Decimal("100") + Decimal(index) / 100,
            "high": Decimal("101") + Decimal(index) / 100,
            "low": Decimal("99") + Decimal(index) / 100,
            "close": Decimal("100") + Decimal(index) / 100,
            "volume": Decimal("1000000"),
            "change_pct": Decimal("0.01"),
        }
        for index, day in enumerate(sessions)
    ]
    lineage = SourceLineage(
        capability="fundamental.profile",
        provider_id="fixture-profile",
        source="fixture",
        row_count=1,
        observed_at=NOW,
        fetched_at=NOW,
        response_digest="a" * 64,
    )
    return industry_rows, lineage


def test_industry_context_requires_a_real_mapping(session):
    result = SelectedStockIndustryContextProvider(session).resolve(
        symbol="300308",
        industry_name=None,
        analysis_date=END,
        industry_rows=None,
        profile_lineage=None,
    )
    assert result.status == ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE
    assert result.role == StockRole.UNKNOWN
    assert result.reason_codes == ("INDUSTRY_MAPPING_UNAVAILABLE",)


def test_industry_context_computes_member_coverage_and_leader_evidence(session):
    rows, lineage = _seed_industry(session)
    result = SelectedStockIndustryContextProvider(session).resolve(
        symbol="300308",
        industry_name="fixture-industry",
        analysis_date=END,
        industry_rows=rows,
        profile_lineage=lineage,
    )
    assert result.status == ContextStatus.AVAILABLE
    assert result.quality_status == "SINGLE_SOURCE"
    assert result.history_row_count == 130
    assert result.constituent_count == 5
    assert result.valid_member_count == 5
    assert result.coverage_ratio == Decimal("1")
    assert result.role == StockRole.LEADER
    assert result.role_evidence.return_rank_20 == 1
    assert result.role_evidence.return_rank_60 == 1
    assert result.role_evidence.amount_rank == 1


def test_low_member_coverage_cannot_produce_a_role(session):
    rows, lineage = _seed_industry(session, ready_members=3)
    result = SelectedStockIndustryContextProvider(session).resolve(
        symbol="300308",
        industry_name="fixture-industry",
        analysis_date=END,
        industry_rows=rows,
        profile_lineage=lineage,
    )
    assert result.coverage_ratio == Decimal("0.6")
    assert result.status == ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE
    assert result.role == StockRole.UNKNOWN
    assert result.role_evidence.reason_code == "ROLE_EVIDENCE_INSUFFICIENT"


def _evidence(**changes) -> StockRoleEvidence:
    values = {
        "member_count": 10,
        "valid_member_count": 10,
        "coverage_ratio": Decimal("1"),
        "return_rank_20": 6,
        "return_rank_60": 6,
        "amount_rank": 5,
        "amount_percentile": Decimal("0.5"),
        "industry_weight": Decimal("0.01"),
        "excess_return_20": Decimal("-0.01"),
        "excess_return_60": Decimal("-0.01"),
        "consecutive_leading_days": 0,
        "evidence_complete": True,
        "reason_code": "ROLE_EVIDENCE_COMPLETE",
    }
    values.update(changes)
    return StockRoleEvidence(**values)


def test_role_classifier_requires_distinct_structured_evidence():
    assert classify_role(
        _evidence(
            return_rank_20=1,
            return_rank_60=1,
            amount_percentile=Decimal("0.95"),
            excess_return_20=Decimal("0.10"),
            excess_return_60=Decimal("0.10"),
            consecutive_leading_days=5,
        )
    ) == StockRole.LEADER
    assert classify_role(
        _evidence(
            amount_percentile=Decimal("0.95"),
            industry_weight=Decimal("0.08"),
        )
    ) == StockRole.CAPACITY_CORE
    assert classify_role(_evidence()) == StockRole.FOLLOWER
    assert classify_role(
        _evidence(evidence_complete=False, reason_code="ROLE_EVIDENCE_INSUFFICIENT")
    ) == StockRole.UNKNOWN
