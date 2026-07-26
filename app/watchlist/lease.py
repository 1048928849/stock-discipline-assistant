from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import WatchlistMonitorLease


LEASE_NAME = "watchlist_monitor"


def _utc_naive(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current
    return current.astimezone(timezone.utc).replace(tzinfo=None)


def acquire_monitor_lease(
    db: Session,
    *,
    owner_token: str,
    lease_seconds: int,
    lease_name: str = LEASE_NAME,
    now: datetime | None = None,
) -> bool:
    if not owner_token or len(owner_token) > 64:
        raise ValueError("owner_token must contain 1 to 64 characters")
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    if not lease_name or len(lease_name) > 64:
        raise ValueError("lease_name must contain 1 to 64 characters")
    acquired_at = _utc_naive(now)
    acquired_until = acquired_at + timedelta(seconds=lease_seconds)
    dialect = db.get_bind().dialect.name
    values = {
        "name": lease_name,
        "owner_token": owner_token,
        "acquired_until": acquired_until,
        "lease_version": 1,
        "updated_at": acquired_at,
    }
    if dialect == "sqlite":
        statement = sqlite_insert(WatchlistMonitorLease).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[WatchlistMonitorLease.name],
            set_={
                "owner_token": owner_token,
                "acquired_until": acquired_until,
                "lease_version": WatchlistMonitorLease.lease_version + 1,
                "updated_at": acquired_at,
            },
            where=(
                (WatchlistMonitorLease.acquired_until.is_(None))
                | (WatchlistMonitorLease.acquired_until <= acquired_at)
                | (WatchlistMonitorLease.owner_token == owner_token)
            ),
        )
    elif dialect == "mysql":
        insert_result = db.execute(
            mysql_insert(WatchlistMonitorLease).values(**values).prefix_with("IGNORE")
        )
        if insert_result.rowcount:
            db.commit()
            return True
        available = (
            (WatchlistMonitorLease.acquired_until.is_(None))
            | (WatchlistMonitorLease.acquired_until <= acquired_at)
            | (WatchlistMonitorLease.owner_token == owner_token)
        )
        result = db.execute(
            update(WatchlistMonitorLease)
            .where(WatchlistMonitorLease.name == lease_name, available)
            .values(
                owner_token=owner_token,
                acquired_until=acquired_until,
                lease_version=WatchlistMonitorLease.lease_version + 1,
                updated_at=acquired_at,
            )
        )
        db.commit()
        return bool(result.rowcount)
    else:
        result = db.execute(
            update(WatchlistMonitorLease)
            .where(
                WatchlistMonitorLease.name == lease_name,
                (
                    WatchlistMonitorLease.acquired_until.is_(None)
                    | (WatchlistMonitorLease.acquired_until <= acquired_at)
                    | (WatchlistMonitorLease.owner_token == owner_token)
                ),
            )
            .values(
                owner_token=owner_token,
                acquired_until=acquired_until,
                lease_version=WatchlistMonitorLease.lease_version + 1,
                updated_at=acquired_at,
            )
        )
        if not result.rowcount:
            db.add(WatchlistMonitorLease(**values))
        db.commit()
        return True
    result = db.execute(statement)
    db.commit()
    return bool(result.rowcount)


def release_monitor_lease(
    db: Session,
    *,
    owner_token: str,
    lease_name: str = LEASE_NAME,
) -> bool:
    result = db.execute(
        update(WatchlistMonitorLease)
        .where(
            WatchlistMonitorLease.name == lease_name,
            WatchlistMonitorLease.owner_token == owner_token,
        )
        .values(owner_token=None, acquired_until=None, updated_at=_utc_naive())
    )
    db.commit()
    return bool(result.rowcount)


__all__ = ["acquire_monitor_lease", "release_monitor_lease"]
