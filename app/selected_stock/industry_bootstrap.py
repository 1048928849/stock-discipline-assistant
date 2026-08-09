from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_hub.trading_calendar import TradingCalendar, get_trading_calendar
from app.domain.market_symbols import CSI300_INTERNAL_SYMBOL
from app.history.contracts import HistoryRequirementPlan
from app.history.service import HistoricalDataBootstrapService, HistoryPlanningBlocked
from app.models import (
    DataQualityRecord,
    HistoricalDataBootstrapRun,
    IndustryConstituentSnapshot,
    IndustryTaxonomyBinding,
)


PURPOSE = "INDUSTRY_ROLE_EVIDENCE"
MINIMUM_ROWS = 120


class IndustryEvidenceBootstrap:
    """Resumable constituent-history preparation for one verified taxonomy binding."""

    def __init__(
        self,
        db: Session,
        *,
        history_service: HistoricalDataBootstrapService | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self.db = db
        self.calendar = calendar or get_trading_calendar()
        self._binding: IndustryTaxonomyBinding | None = None
        self.history_service = history_service or HistoricalDataBootstrapService(
            db,
            calendar=self.calendar,
            plan_factory=self._plan,
            purpose=PURPOSE,
        )

    def _start_date(self, end: date, sessions: int = 250) -> date:
        selected = 0
        current = end
        while selected < sessions:
            if self.calendar.is_session(current):
                selected += 1
            current -= timedelta(days=1)
        return current + timedelta(days=1)

    def _plan(self, *, trade_date: date, now: datetime) -> HistoryRequirementPlan:
        del now
        binding = self._binding
        if binding is None:
            raise HistoryPlanningBlocked("INDUSTRY_TAXONOMY_BINDING_MISSING")
        record = self.db.get(DataQualityRecord, binding.quality_record_id)
        if (
            record is None
            or record.provider_id != binding.provider_id
            or record.quality_status not in {"VERIFIED", "SINGLE_SOURCE"}
            or not record.persisted
        ):
            raise HistoryPlanningBlocked("INDUSTRY_TAXONOMY_MISMATCH")
        rows = list(
            self.db.scalars(
                select(IndustryConstituentSnapshot)
                .join(
                    DataQualityRecord,
                    DataQualityRecord.id == IndustryConstituentSnapshot.quality_record_id,
                )
                .where(
                    IndustryConstituentSnapshot.industry_key
                    == binding.provider_industry_id,
                    IndustryConstituentSnapshot.snapshot_date == trade_date,
                    DataQualityRecord.provider_id == binding.provider_id,
                    DataQualityRecord.persisted.is_(True),
                    DataQualityRecord.quality_status.in_(("VERIFIED", "SINGLE_SOURCE")),
                )
                .order_by(IndustryConstituentSnapshot.symbol)
            )
        )
        symbols = tuple(sorted({row.symbol for row in rows}))
        if not symbols:
            raise HistoryPlanningBlocked("INDUSTRY_CONSTITUENTS_UNAVAILABLE")
        if binding.symbol not in symbols:
            raise HistoryPlanningBlocked("INDUSTRY_MEMBERSHIP_CONFLICTED")
        return HistoryRequirementPlan.create(
            trade_date=trade_date,
            benchmark_symbols=(CSI300_INTERNAL_SYMBOL,),
            selected_industries=(binding.provider_industry_id,),
            required_stock_symbols=symbols,
            required_capabilities=("market.daily.qfq", "market.turnover.daily"),
            start_date=self._start_date(trade_date),
            end_date=trade_date,
            minimum_rows=MINIMUM_ROWS,
            config={
                "purpose": PURPOSE,
                "provider_id": binding.provider_id,
                "classification_system": binding.classification_system,
                "provider_industry_id": binding.provider_industry_id,
                "minimum_coverage": "0.80",
            },
        )

    def run(
        self,
        binding: IndustryTaxonomyBinding,
        *,
        force_refresh: bool = False,
        now: datetime | None = None,
    ) -> HistoricalDataBootstrapRun:
        self._binding = binding
        return self.history_service.run(
            trade_date=binding.effective_date,
            force_refresh=force_refresh,
            now=now,
        )


__all__ = ["IndustryEvidenceBootstrap", "MINIMUM_ROWS", "PURPOSE"]
