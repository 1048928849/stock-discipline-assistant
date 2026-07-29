from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.composition.data_hub import build_data_hub
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    resolve_analysis_trade_date,
    shanghai_now,
    to_shanghai_aware,
)
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import canonical_semantic_key
from app.models import DataQualityRecord, MarketBreadthSnapshot
from app.services.product_data import persist_product_result


@dataclass(frozen=True)
class MarketBreadthCaptureResult:
    trade_date: date
    quality_record_id: int
    normalized_digest: str
    reused: bool


def _existing_capture(
    db: Session,
    trade_date: date,
) -> MarketBreadthCaptureResult | None:
    row = db.scalar(
        select(MarketBreadthSnapshot).where(
            MarketBreadthSnapshot.market_id == "CN-A",
            MarketBreadthSnapshot.trade_date == trade_date,
        )
    )
    if row is None:
        return None
    quality = db.get(DataQualityRecord, row.quality_record_id)
    if (
        quality is None
        or quality.capability != "market.breadth.daily"
        or quality.subject_type != "market"
        or quality.subject_id != "CN-A"
        or canonical_semantic_key(quality.semantic_key) != "daily/all-a"
        or not quality.persisted
        or not quality.trusted
        or not quality.normalized_digest
    ):
        return None
    return MarketBreadthCaptureResult(
        trade_date=trade_date,
        quality_record_id=quality.id,
        normalized_digest=quality.normalized_digest,
        reused=True,
    )


def capture_market_breadth(
    db: Session,
    trade_date: date,
    *,
    router: DataHubRouter | None = None,
    calendar: TradingCalendar | None = None,
    evaluated_at: datetime | None = None,
    force_refresh: bool = False,
) -> MarketBreadthCaptureResult:
    calendar = calendar or get_trading_calendar()
    current = to_shanghai_aware(
        evaluated_at or shanghai_now(),
        naive_is_shanghai=evaluated_at is not None and evaluated_at.tzinfo is None,
    )
    latest = resolve_analysis_trade_date(calendar, current)
    is_session = getattr(calendar, "is_session", lambda day: day == latest)
    if not is_session(trade_date) or trade_date > latest:
        raise ProviderUnavailableError(
            "market breadth capture requires a completed trading session"
        )
    if not force_refresh:
        existing = _existing_capture(db, trade_date)
        if existing is not None:
            return existing
    active_router = router or build_data_hub(
        db,
        calendar=calendar,
        now_fn=lambda: current,
    )
    result = active_router.get_market_breadth(trade_date)
    if result.quality_status not in {
        DataQualityStatus.SINGLE_SOURCE,
        DataQualityStatus.VERIFIED,
    } or not result.value:
        db.commit()
        detail = "; ".join(result.errors) or result.quality_status.value
        raise ProviderUnavailableError(f"market breadth capture failed: {detail}")
    try:
        persist_product_result(db, active_router, result)
    except Exception:
        db.commit()
        raise
    db.commit()
    if result.quality_record_id is None or result.normalized_digest is None:
        raise ProviderUnavailableError("market breadth capture lineage is incomplete")
    return MarketBreadthCaptureResult(
        trade_date=trade_date,
        quality_record_id=result.quality_record_id,
        normalized_digest=result.normalized_digest,
        reused=False,
    )


def capture_latest_market_breadth(
    db: Session,
    *,
    router: DataHubRouter | None = None,
    calendar: TradingCalendar | None = None,
    evaluated_at: datetime | None = None,
    force_refresh: bool = False,
) -> MarketBreadthCaptureResult:
    calendar = calendar or get_trading_calendar()
    current = to_shanghai_aware(
        evaluated_at or shanghai_now(),
        naive_is_shanghai=evaluated_at is not None and evaluated_at.tzinfo is None,
    )
    trade_date = resolve_analysis_trade_date(calendar, current)
    return capture_market_breadth(
        db,
        trade_date,
        router=router,
        calendar=calendar,
        evaluated_at=current,
        force_refresh=force_refresh,
    )


__all__ = [
    "MarketBreadthCaptureResult",
    "capture_latest_market_breadth",
    "capture_market_breadth",
]
