from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.data_hub.contracts import DailyBar, ProviderUnavailableError, TurnoverDaily
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import to_market_storage_naive
from app.domain.quality_subject import SubjectRef
from app.models import MarketDailyBar, MarketTurnoverSnapshot
from app.services.market_cache import canonical_series_row, mapping_series_bars


def _daily_rows(result: ProviderResult, subject: SubjectRef) -> list[DailyBar]:
    value = result.require_trusted_value()
    if isinstance(value, list) and all(isinstance(row, DailyBar) for row in value):
        rows = list(value)
    elif isinstance(value, dict) and isinstance(value.get("rows"), list):
        rows = mapping_series_bars(
            value,
            cache_symbol=subject.subject_id,
            adjustment=subject.semantic_key.split("/", 1)[0],
        )
    else:
        raise ProviderUnavailableError("history persistence requires daily bars")
    return rows


def _validate_daily(
    result: ProviderResult,
    subject: SubjectRef,
    *,
    minimum_rows: int,
    requested_start: date,
    requested_end: date,
) -> list[DailyBar]:
    if result.subject != subject or result.quality_record_id is None:
        raise ProviderUnavailableError("history persistence lineage scope mismatch")
    rows = _daily_rows(result, subject)
    if len(rows) < minimum_rows:
        raise ProviderUnavailableError("history persistence rows are insufficient")
    dates = [row.trade_date for row in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ProviderUnavailableError("history persistence dates must be unique and ordered")
    if dates[0] < requested_start or dates[-1] != requested_end:
        raise ProviderUnavailableError("history persistence window does not match request")
    try:
        canonical = [canonical_series_row(row) for row in rows]
        expected = [canonical_series_row(row) for row in _daily_rows(result, subject)]
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ProviderUnavailableError("history persistence payload mismatch") from exc
    if canonical != expected:
        raise ProviderUnavailableError("history persistence payload mismatch")
    return rows


def _validate_turnover(
    result: ProviderResult,
    *,
    minimum_rows: int,
    requested_start: date,
    requested_end: date,
) -> list[TurnoverDaily]:
    value = result.require_trusted_value()
    if not isinstance(value, list) or not all(isinstance(row, TurnoverDaily) for row in value):
        raise ProviderUnavailableError("turnover persistence requires native rows")
    rows = list(value)
    if len(rows) < minimum_rows:
        raise ProviderUnavailableError("turnover persistence rows are insufficient")
    dates = [row.trade_date for row in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ProviderUnavailableError("turnover persistence dates must be unique and ordered")
    if dates[0] < requested_start or dates[-1] != requested_end:
        raise ProviderUnavailableError("turnover persistence window does not match request")
    for row in rows:
        if (
            row.symbol != result.subject.subject_id
            or row.amount is None
            or not isinstance(row.amount, Decimal)
            or row.amount < 0
            or row.turnover_rate < 0
            or row.observed_at.tzinfo is None
            or row.fetched_at.tzinfo is None
        ):
            raise ProviderUnavailableError("turnover persistence row is invalid")
    return rows


def _validate_lineage(router: DataHubRouter, result: ProviderResult, row_count: int) -> None:
    record = router.validate_persistence_result(result)
    if record.row_count != row_count:
        raise ProviderUnavailableError("history quality row count does not match payload")


def _store_daily(
    db: Session,
    result: ProviderResult,
    rows: list[DailyBar],
    *,
    requested_start: date,
    requested_end: date,
) -> None:
    sample = rows[0]
    db.execute(
        delete(MarketDailyBar).where(
            MarketDailyBar.symbol == sample.symbol,
            MarketDailyBar.source == sample.source,
            MarketDailyBar.adjustment == sample.adjustment,
            MarketDailyBar.price_unit == sample.price_unit,
            MarketDailyBar.volume_unit == sample.volume_unit,
            MarketDailyBar.trade_date >= requested_start,
            MarketDailyBar.trade_date <= requested_end,
        )
    )
    db.add_all(
        [
            MarketDailyBar(
                symbol=row.symbol,
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                adjustment=row.adjustment,
                price_unit=row.price_unit,
                volume_unit=row.volume_unit,
                observed_at=to_market_storage_naive(row.observed_at),
                quality_status=result.quality_status.value,
                quality_record_id=result.quality_record_id,
                source=row.source,
                fetched_at=to_market_storage_naive(row.fetched_at),
            )
            for row in rows
        ]
    )


def persist_stock_history_bundle(
    db: Session,
    router: DataHubRouter,
    *,
    daily_result: ProviderResult,
    turnover_result: ProviderResult,
    requested_start: date,
    requested_end: date,
    minimum_rows: int,
) -> int:
    daily_subject = daily_result.subject
    if daily_subject is None or turnover_result.subject is None:
        raise ProviderUnavailableError("stock history bundle requires subjects")
    if daily_subject.subject_id != turnover_result.subject.subject_id:
        raise ProviderUnavailableError("stock history bundle subject mismatch")
    daily = _validate_daily(
        daily_result,
        daily_subject,
        minimum_rows=minimum_rows,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    turnover = _validate_turnover(
        turnover_result,
        minimum_rows=minimum_rows,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    if {row.trade_date for row in daily} != {row.trade_date for row in turnover}:
        raise ProviderUnavailableError("daily and turnover trade dates do not match")
    _validate_lineage(router, daily_result, len(daily))
    _validate_lineage(router, turnover_result, len(turnover))
    with db.begin_nested():
        _store_daily(
            db,
            daily_result,
            daily,
            requested_start=requested_start,
            requested_end=requested_end,
        )
        db.execute(
            delete(MarketTurnoverSnapshot).where(
                MarketTurnoverSnapshot.symbol == daily_subject.subject_id,
                MarketTurnoverSnapshot.trade_date >= requested_start,
                MarketTurnoverSnapshot.trade_date <= requested_end,
            )
        )
        db.add_all(
            [
                MarketTurnoverSnapshot(
                    symbol=row.symbol,
                    trade_date=row.trade_date,
                    turnover_rate=row.turnover_rate,
                    amount=row.amount,
                    observed_at=to_market_storage_naive(row.observed_at),
                    source=row.source,
                    fetched_at=to_market_storage_naive(row.fetched_at),
                    quality_record_id=turnover_result.quality_record_id,
                )
                for row in turnover
            ]
        )
        db.flush()
        router.mark_persisted(daily_result)
        router.mark_persisted(turnover_result)
    return len(daily) + len(turnover)


def persist_stock_daily_window(
    db: Session,
    router: DataHubRouter,
    *,
    result: ProviderResult,
    expected_trade_dates: set[date],
    requested_start: date,
    requested_end: date,
    minimum_rows: int,
) -> int:
    if result.subject is None:
        raise ProviderUnavailableError("stock daily persistence requires a subject")
    rows = _validate_daily(
        result,
        result.subject,
        minimum_rows=minimum_rows,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    if {row.trade_date for row in rows} != expected_trade_dates:
        raise ProviderUnavailableError("daily and turnover trade dates do not match")
    _validate_lineage(router, result, len(rows))
    with db.begin_nested():
        _store_daily(
            db,
            result,
            rows,
            requested_start=requested_start,
            requested_end=requested_end,
        )
        db.flush()
        router.mark_persisted(result)
    return len(rows)


def persist_turnover_history_window(
    db: Session,
    router: DataHubRouter,
    *,
    result: ProviderResult,
    expected_trade_dates: set[date],
    requested_start: date,
    requested_end: date,
    minimum_rows: int,
) -> int:
    rows = _validate_turnover(
        result,
        minimum_rows=minimum_rows,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    if {row.trade_date for row in rows} != expected_trade_dates:
        raise ProviderUnavailableError("daily and turnover trade dates do not match")
    _validate_lineage(router, result, len(rows))
    with db.begin_nested():
        db.execute(
            delete(MarketTurnoverSnapshot).where(
                MarketTurnoverSnapshot.symbol == result.subject.subject_id,
                MarketTurnoverSnapshot.trade_date >= requested_start,
                MarketTurnoverSnapshot.trade_date <= requested_end,
            )
        )
        db.add_all(
            [
                MarketTurnoverSnapshot(
                    symbol=row.symbol,
                    trade_date=row.trade_date,
                    turnover_rate=row.turnover_rate,
                    amount=row.amount,
                    observed_at=to_market_storage_naive(row.observed_at),
                    source=row.source,
                    fetched_at=to_market_storage_naive(row.fetched_at),
                    quality_record_id=result.quality_record_id,
                )
                for row in rows
            ]
        )
        db.flush()
        router.mark_persisted(result)
    return len(rows)


def persist_index_history_window(
    db: Session,
    router: DataHubRouter,
    *,
    result: ProviderResult,
    subject: SubjectRef,
    requested_start: date,
    requested_end: date,
    minimum_rows: int,
) -> int:
    rows = _validate_daily(
        result,
        subject,
        minimum_rows=minimum_rows,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    _validate_lineage(router, result, len(rows))
    with db.begin_nested():
        _store_daily(
            db,
            result,
            rows,
            requested_start=requested_start,
            requested_end=requested_end,
        )
        db.flush()
        router.mark_persisted(result)
    return len(rows)


__all__ = [
    "persist_index_history_window",
    "persist_stock_daily_window",
    "persist_stock_history_bundle",
    "persist_turnover_history_window",
]
