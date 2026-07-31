from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.market_subjects import (
    industry_constituents_subject,
    stock_daily_subject,
)
from app.data_hub.quality import DataQualityStatus
from app.data_hub.trading_calendar import TradingCalendar, get_trading_calendar
from app.domain.hashing import canonical_hash
from app.models import (
    DataQualityRecord,
    IndustryConstituentSnapshot,
    MarketDailyBar,
    MarketTurnoverSnapshot,
)
from app.selected_stock.contracts import (
    ContextStatus,
    IndustryContextEvidence,
    IndustryMapping,
    SourceLineage,
    StockRole,
    StockRoleEvidence,
)


ZERO = Decimal("0")
ONE = Decimal("1")
TRUSTED = {
    DataQualityStatus.VERIFIED.value,
    DataQualityStatus.SINGLE_SOURCE.value,
}
MINIMUM_INDUSTRY_ROWS = 120
MINIMUM_MEMBER_COVERAGE = Decimal("0.80")


def _return(rows: list[MarketDailyBar], sessions: int) -> Decimal | None:
    if len(rows) <= sessions or rows[-sessions - 1].close <= 0:
        return None
    return rows[-1].close / rows[-sessions - 1].close - ONE


def _drawdown(rows: list[MarketDailyBar], sessions: int = 60) -> Decimal | None:
    closes = [row.close for row in rows[-sessions:]]
    if not closes:
        return None
    peak = closes[0]
    worst = ZERO
    for close in closes:
        peak = max(peak, close)
        if peak > 0:
            worst = min(worst, close / peak - ONE)
    return worst


def _rank(values: dict[str, Decimal], symbol: str, *, reverse: bool = True) -> int | None:
    if symbol not in values:
        return None
    ordered = sorted(values, key=lambda key: (values[key], key), reverse=reverse)
    return ordered.index(symbol) + 1


def classify_role(evidence: StockRoleEvidence) -> StockRole:
    if not evidence.evidence_complete:
        return StockRole.UNKNOWN
    top_ten = max(1, (evidence.valid_member_count + 9) // 10)
    top_twenty = max(1, (evidence.valid_member_count + 4) // 5)
    top_thirty = max(1, (evidence.valid_member_count * 3 + 9) // 10)
    if (
        evidence.return_rank_20 is not None
        and evidence.return_rank_20 <= top_ten
        and evidence.return_rank_60 is not None
        and evidence.return_rank_60 <= top_twenty
        and evidence.amount_percentile is not None
        and evidence.amount_percentile >= Decimal("0.70")
        and evidence.consecutive_leading_days >= 3
        and (evidence.excess_return_20 or ZERO) > 0
    ):
        return StockRole.LEADER
    if (
        evidence.amount_percentile is not None
        and evidence.amount_percentile >= Decimal("0.90")
        and (
            (evidence.industry_weight or ZERO) >= Decimal("0.05")
            or (evidence.return_rank_60 or evidence.valid_member_count + 1) <= top_thirty
        )
    ):
        return StockRole.CAPACITY_CORE
    if (
        evidence.return_rank_20 is not None
        and evidence.return_rank_20 <= top_twenty
        and (evidence.excess_return_20 or ZERO) > 0
        and (evidence.excess_return_60 or ZERO) > 0
    ):
        return StockRole.TREND_CORE
    if (evidence.excess_return_20 or ZERO) > 0 and (
        evidence.excess_return_60 or ZERO
    ) <= 0:
        return StockRole.ROTATION_FRONT
    if (
        evidence.return_rank_20 is not None
        and evidence.return_rank_20 > max(1, evidence.valid_member_count // 2)
        and (evidence.excess_return_20 or ZERO) < 0
    ):
        return StockRole.FOLLOWER
    return StockRole.UNKNOWN


def _empty(reason: str, *, mapping: IndustryMapping | None = None) -> IndustryContextEvidence:
    evidence = StockRoleEvidence(
        member_count=0,
        valid_member_count=0,
        coverage_ratio=ZERO,
        evidence_complete=False,
        reason_code=reason,
    )
    return IndustryContextEvidence(
        status=ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE,
        mapping=mapping,
        history_row_count=0,
        constituent_count=0,
        valid_member_count=0,
        coverage_ratio=ZERO,
        quality_status=DataQualityStatus.MISSING.value,
        role=StockRole.UNKNOWN,
        role_evidence=evidence,
        reason_codes=(reason,),
    )


class SelectedStockIndustryContextProvider:
    """Build selected-stock industry evidence from exact persisted DataHub lineage."""

    def __init__(self, db: Session, *, calendar: TradingCalendar | None = None) -> None:
        self.db = db
        self.calendar = calendar or get_trading_calendar()

    def _constituents(
        self, industry: str, analysis_date: date
    ) -> tuple[list[IndustryConstituentSnapshot], DataQualityRecord | None]:
        subject = industry_constituents_subject(industry)
        record = self.db.scalar(
            select(DataQualityRecord)
            .where(
                DataQualityRecord.capability == "market.industry.constituents",
                DataQualityRecord.subject_type == subject.subject_type,
                DataQualityRecord.subject_id == subject.subject_id,
                DataQualityRecord.semantic_key == subject.semantic_key,
                DataQualityRecord.persisted.is_(True),
                DataQualityRecord.quality_status.in_(TRUSTED),
            )
            .order_by(DataQualityRecord.observed_at.desc(), DataQualityRecord.id.desc())
        )
        if record is None:
            return [], None
        rows = list(
            self.db.scalars(
                select(IndustryConstituentSnapshot)
                .where(
                    IndustryConstituentSnapshot.quality_record_id == record.id,
                    IndustryConstituentSnapshot.snapshot_date <= analysis_date,
                )
                .order_by(IndustryConstituentSnapshot.symbol)
            )
        )
        if not rows or max(row.snapshot_date for row in rows) != analysis_date:
            return [], record
        return rows, record

    def _member_histories(
        self, symbols: list[str], analysis_date: date
    ) -> tuple[dict[str, list[MarketDailyBar]], tuple[int, ...]]:
        if not symbols:
            return {}, ()
        semantic_key = stock_daily_subject(symbols[0], "qfq", "CNY", "share").semantic_key
        records = list(
            self.db.scalars(
                select(DataQualityRecord)
                .where(
                    DataQualityRecord.capability == "market.daily.qfq",
                    DataQualityRecord.subject_type == "stock",
                    DataQualityRecord.subject_id.in_(symbols),
                    DataQualityRecord.semantic_key == semantic_key,
                    DataQualityRecord.persisted.is_(True),
                    DataQualityRecord.quality_status.in_(TRUSTED),
                )
                .order_by(
                    DataQualityRecord.subject_id,
                    DataQualityRecord.observed_at.desc(),
                    DataQualityRecord.id.desc(),
                )
            )
        )
        selected: dict[str, DataQualityRecord] = {}
        for record in records:
            selected.setdefault(record.subject_id, record)
        bars = list(
            self.db.scalars(
                select(MarketDailyBar)
                .where(
                    MarketDailyBar.quality_record_id.in_(
                        [record.id for record in selected.values()]
                    ),
                    MarketDailyBar.trade_date <= analysis_date,
                )
                .order_by(MarketDailyBar.symbol, MarketDailyBar.trade_date)
            )
        ) if selected else []
        grouped: dict[str, list[MarketDailyBar]] = defaultdict(list)
        for bar in bars:
            grouped[bar.symbol].append(bar)
        valid = {
            symbol: rows
            for symbol, rows in grouped.items()
            if len(rows) >= 61 and rows[-1].trade_date == analysis_date
        }
        ids = tuple(sorted(selected[symbol].id for symbol in valid))
        return valid, ids

    def _amounts(self, symbols: list[str], analysis_date: date) -> dict[str, Decimal]:
        if not symbols:
            return {}
        rows = self.db.execute(
            select(MarketTurnoverSnapshot, DataQualityRecord)
            .join(
                DataQualityRecord,
                DataQualityRecord.id == MarketTurnoverSnapshot.quality_record_id,
            )
            .where(
                MarketTurnoverSnapshot.symbol.in_(symbols),
                MarketTurnoverSnapshot.trade_date == analysis_date,
                MarketTurnoverSnapshot.amount.is_not(None),
                DataQualityRecord.persisted.is_(True),
                DataQualityRecord.quality_status.in_(TRUSTED),
            )
        ).all()
        return {
            row.symbol: row.amount
            for row, _record in rows
            if row.amount is not None and row.amount >= 0
        }

    def resolve(
        self,
        *,
        symbol: str,
        industry_name: str | None,
        analysis_date: date,
        industry_rows: list[dict[str, Any]] | None,
        profile_lineage: SourceLineage | None,
    ) -> IndustryContextEvidence:
        if not industry_name or profile_lineage is None:
            return _empty("INDUSTRY_MAPPING_UNAVAILABLE")
        mapping = IndustryMapping(
            symbol=symbol,
            industry_id=industry_constituents_subject(industry_name).subject_id,
            industry_name=industry_name,
            classification_system="PROVIDER_INDUSTRY_CLASSIFICATION",
            effective_date=analysis_date,
            provider=profile_lineage.provider_id,
            source_reference=profile_lineage.capability,
            fetched_at=profile_lineage.fetched_at or profile_lineage.observed_at,
            response_digest=profile_lineage.response_digest
            or canonical_hash(profile_lineage.model_dump(mode="json")),
        )
        history = list(industry_rows or [])
        if (
            len(history) < MINIMUM_INDUSTRY_ROWS
            or history[-1]["trade_date"] != analysis_date
            or len({row["trade_date"] for row in history}) != len(history)
        ):
            result = _empty("INDUSTRY_HISTORY_INSUFFICIENT_OR_MISALIGNED", mapping=mapping)
            return result.model_copy(update={"history_row_count": len(history)})
        constituents, constituent_record = self._constituents(industry_name, analysis_date)
        if constituent_record is None or not constituents:
            result = _empty("INDUSTRY_CONSTITUENTS_UNAVAILABLE", mapping=mapping)
            return result.model_copy(update={"history_row_count": len(history)})
        symbols = sorted({row.symbol for row in constituents})
        if symbol not in symbols:
            result = _empty("SYMBOL_NOT_IN_INDUSTRY_CONSTITUENTS", mapping=mapping)
            return result.model_copy(
                update={
                    "history_row_count": len(history),
                    "constituent_count": len(symbols),
                    "quality_record_ids": (constituent_record.id,),
                }
            )
        histories, history_record_ids = self._member_histories(symbols, analysis_date)
        coverage = Decimal(len(histories)) / Decimal(len(symbols)) if symbols else ZERO
        amounts = self._amounts(symbols, analysis_date)
        amount_coverage = Decimal(len(amounts)) / Decimal(len(symbols)) if symbols else ZERO
        returns20 = {
            key: value
            for key, rows in histories.items()
            if (value := _return(rows, 20)) is not None
        }
        returns60 = {
            key: value
            for key, rows in histories.items()
            if (value := _return(rows, 60)) is not None
        }
        industry20 = Decimal(str(industry_rows[-1]["close"])) / Decimal(
            str(industry_rows[-21]["close"])
        ) - ONE
        industry60 = Decimal(str(industry_rows[-1]["close"])) / Decimal(
            str(industry_rows[-61]["close"])
        ) - ONE
        drawdowns = {
            key: value
            for key, rows in histories.items()
            if (value := _drawdown(rows)) is not None
        }
        amount_rank = _rank(amounts, symbol)
        target_member = next(row for row in constituents if row.symbol == symbol)
        aligned_industry = {row["trade_date"]: row for row in history[-61:]}
        target_rows = histories.get(symbol, [])[-61:]
        leading = 0
        up_diffs: list[Decimal] = []
        down_diffs: list[Decimal] = []
        prior_close = None
        for row in target_rows:
            industry_row = aligned_industry.get(row.trade_date)
            if prior_close and industry_row is not None:
                stock_change = row.close / prior_close - ONE
                industry_change = Decimal(str(industry_row.get("change_pct") or 0)) / Decimal("100")
                difference = stock_change - industry_change
                (up_diffs if industry_change >= 0 else down_diffs).append(difference)
            prior_close = row.close
        for row in reversed(target_rows[1:]):
            industry_row = aligned_industry.get(row.trade_date)
            previous = target_rows[target_rows.index(row) - 1]
            if industry_row is None or previous.close <= 0:
                break
            stock_change = row.close / previous.close - ONE
            industry_change = Decimal(str(industry_row.get("change_pct") or 0)) / Decimal("100")
            if stock_change <= industry_change:
                break
            leading += 1
        complete = (
            coverage >= MINIMUM_MEMBER_COVERAGE
            and amount_coverage >= MINIMUM_MEMBER_COVERAGE
            and symbol in histories
            and symbol in amounts
        )
        reason = "ROLE_EVIDENCE_COMPLETE" if complete else "ROLE_EVIDENCE_INSUFFICIENT"
        evidence = StockRoleEvidence(
            member_count=len(symbols),
            valid_member_count=len(histories),
            coverage_ratio=coverage,
            return_rank_20=_rank(returns20, symbol),
            return_rank_60=_rank(returns60, symbol),
            amount_rank=amount_rank,
            amount_percentile=(
                ONE - (Decimal(amount_rank - 1) / Decimal(max(1, len(amounts))))
                if amount_rank is not None
                else None
            ),
            industry_weight=target_member.weight,
            up_day_elasticity=sum(up_diffs, ZERO) / Decimal(len(up_diffs))
            if up_diffs
            else None,
            down_day_resilience=sum(down_diffs, ZERO) / Decimal(len(down_diffs))
            if down_diffs
            else None,
            excess_return_20=returns20.get(symbol) - industry20
            if symbol in returns20
            else None,
            excess_return_60=returns60.get(symbol) - industry60
            if symbol in returns60
            else None,
            max_drawdown_rank_60=_rank(drawdowns, symbol),
            liquidity=amounts.get(symbol),
            consecutive_leading_days=leading,
            evidence_complete=complete,
            reason_code=reason,
        )
        role = classify_role(evidence)
        status = ContextStatus.AVAILABLE if complete else ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE
        return IndustryContextEvidence(
            status=status,
            mapping=mapping,
            history_row_count=len(history),
            constituent_count=len(symbols),
            valid_member_count=len(histories),
            coverage_ratio=coverage,
            quality_status=constituent_record.quality_status,
            role=role,
            role_evidence=evidence,
            reason_codes=(reason,),
            quality_record_ids=(constituent_record.id,) + history_record_ids,
        )


__all__ = [
    "MINIMUM_INDUSTRY_ROWS",
    "MINIMUM_MEMBER_COVERAGE",
    "SelectedStockIndustryContextProvider",
    "classify_role",
]
