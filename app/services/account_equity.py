from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, AccountEquitySnapshot


@dataclass(frozen=True)
class AccountDrawdownSnapshot:
    current_equity: Decimal
    peak_equity: Decimal
    drawdown_pct: float
    observation_count: int


def record_account_equity(
    db: Session,
    account: Account,
    *,
    snapshot_date: date | None = None,
    source: str = "account_update",
) -> AccountEquitySnapshot:
    day = snapshot_date or date.today()  # noqa: DTZ011 - account snapshots are local dates
    item = db.scalar(
        select(AccountEquitySnapshot).where(
            AccountEquitySnapshot.account_id == account.id,
            AccountEquitySnapshot.snapshot_date == day,
        )
    )
    if item is None:
        item = AccountEquitySnapshot(
            account_id=account.id,
            snapshot_date=day,
            equity=account.total_assets,
            source=source,
        )
        db.add(item)
    else:
        # 同日多次更新保留峰值；当前权益始终从Account读取。
        item.equity = max(item.equity, account.total_assets)
        item.source = source
    db.flush()
    return item


def account_drawdown(
    db: Session, account_id: int, *, current_equity: Decimal
) -> AccountDrawdownSnapshot:
    peak, count = db.execute(
        select(func.max(AccountEquitySnapshot.equity), func.count(AccountEquitySnapshot.id)).where(
            AccountEquitySnapshot.account_id == account_id
        )
    ).one()
    peak_equity = max(current_equity, Decimal(peak)) if peak is not None else current_equity
    drawdown = float((peak_equity - current_equity) / peak_equity * 100) if peak_equity > 0 else 0
    return AccountDrawdownSnapshot(current_equity, peak_equity, round(drawdown, 4), int(count))
