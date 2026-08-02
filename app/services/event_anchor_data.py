from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.event_anchor import InstitutionalEvidence
from app.models import InstitutionalTransactionEvidence, MarketMinuteBar


def load_event_anchor_inputs(
    db: Session, symbol: str, *, as_of: date, lookback_days: int = 120
) -> tuple[dict[date, pd.DataFrame], tuple[InstitutionalEvidence, ...]]:
    start = datetime.combine(as_of - timedelta(days=lookback_days), time.min)
    end = datetime.combine(as_of + timedelta(days=1), time.min)
    bars = db.scalars(
        select(MarketMinuteBar)
        .where(
            MarketMinuteBar.symbol == symbol,
            MarketMinuteBar.trade_time >= start,
            MarketMinuteBar.trade_time < end,
        )
        .order_by(MarketMinuteBar.trade_time)
    ).all()
    by_date: dict[date, list[MarketMinuteBar]] = {}
    for item in bars:
        by_date.setdefault(item.trade_time.date(), []).append(item)
    sessions = {
        day: pd.DataFrame(
            {
                "Open": [float(item.open) for item in items],
                "High": [float(item.high) for item in items],
                "Low": [float(item.low) for item in items],
                "Close": [float(item.close) for item in items],
                "Volume": [float(item.volume) for item in items],
                "Amount": [
                    float(item.amount) if item.amount is not None else None for item in items
                ],
            },
            index=pd.to_datetime([item.trade_time for item in items]),
        )
        for day, items in by_date.items()
    }
    records = db.scalars(
        select(InstitutionalTransactionEvidence)
        .where(
            InstitutionalTransactionEvidence.symbol == symbol,
            InstitutionalTransactionEvidence.event_date >= start.date(),
            InstitutionalTransactionEvidence.event_date <= as_of,
        )
        .order_by(InstitutionalTransactionEvidence.event_date)
    ).all()
    evidence = tuple(
        InstitutionalEvidence(
            item.evidence_key,
            item.event_date,
            item.evidence_type,
            float(item.price) if item.price is not None else None,
            item.source,
            item.institutional,
        )
        for item in records
    )
    return sessions, evidence
