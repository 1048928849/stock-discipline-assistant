from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data_hub.trading_calendar import TradingCalendar, get_trading_calendar
from app.domain.quality import DataQualityStatus, worst_quality


@dataclass(frozen=True)
class CapabilityQualityPolicy:
    capability: str
    required: bool
    max_age: timedelta | None = None
    max_trading_session_lag: int | None = None
    business_fields: tuple[str, ...] = ()
    numeric_tolerance: Decimal = Decimal("0.000001")
    time_tolerance: timedelta = timedelta(seconds=1)
    verify_multiple_sources: bool = False
    allow_cache_fallback: bool = True


DEFAULT_POLICY = CapabilityQualityPolicy(
    capability="default",
    required=False,
    max_age=timedelta(days=7),
)


QUALITY_POLICIES: dict[str, CapabilityQualityPolicy] = {
    "market.quote": CapabilityQualityPolicy(
        "market.quote",
        required=True,
        max_age=timedelta(minutes=30),
        business_fields=("symbol", "price"),
        numeric_tolerance=Decimal("0.0001"),
        verify_multiple_sources=True,
    ),
    "market.daily": CapabilityQualityPolicy(
        "market.daily",
        required=True,
        max_trading_session_lag=0,
        business_fields=(
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "adjustment",
            "price_unit",
            "volume_unit",
        ),
        numeric_tolerance=Decimal("0.0001"),
        verify_multiple_sources=True,
    ),
    "market.index_daily": CapabilityQualityPolicy(
        "market.index_daily",
        required=True,
        max_trading_session_lag=0,
        business_fields=("date", "close", "volume"),
        numeric_tolerance=Decimal("0.0001"),
        verify_multiple_sources=True,
    ),
    "market.sector_daily": CapabilityQualityPolicy(
        "market.sector_daily",
        required=True,
        max_trading_session_lag=0,
        business_fields=("date", "close", "volume"),
        numeric_tolerance=Decimal("0.0001"),
        verify_multiple_sources=True,
    ),
    "fundamental.profile": CapabilityQualityPolicy(
        "fundamental.profile",
        required=True,
        max_age=timedelta(days=30),
        verify_multiple_sources=True,
    ),
    "announcement.catalog": CapabilityQualityPolicy(
        "announcement.catalog",
        required=True,
        max_age=timedelta(hours=24),
        verify_multiple_sources=True,
    ),
    "fundamental.statements": CapabilityQualityPolicy(
        "fundamental.statements", required=False, max_age=timedelta(days=120)
    ),
    "fundamental.valuation": CapabilityQualityPolicy(
        "fundamental.valuation", required=False, max_age=timedelta(days=7)
    ),
    "news.company": CapabilityQualityPolicy(
        "news.company", required=False, max_age=timedelta(hours=24)
    ),
    "social.company_clues": CapabilityQualityPolicy(
        "social.company_clues", required=False, max_age=timedelta(hours=24)
    ),
}


@dataclass(frozen=True)
class QualityObservation:
    provider_id: str
    value: Any
    observed_at: datetime | date | None = None
    fetched_at: datetime | None = None
    stale: bool = False
    normalized_digest: str | None = None


_METADATA_KEYS = {
    "source",
    "source_api",
    "source_name",
    "provider",
    "provider_id",
    "fetched_at",
    "cached_at",
    "cache_used",
    "fallback_used",
    "errors",
}


def policy_for(capability: str) -> CapabilityQualityPolicy:
    return QUALITY_POLICIES.get(
        capability,
        CapabilityQualityPolicy(
            capability=capability,
            required=False,
            max_age=DEFAULT_POLICY.max_age,
        ),
    )


def _normalized_number(value: Any, tolerance: Decimal) -> str:
    number = Decimal(str(value))
    if tolerance > 0:
        number = number.quantize(tolerance)
    normalized = number.normalize()
    return format(normalized, "f")


def _normalize(
    value: Any,
    *,
    policy: CapabilityQualityPolicy,
    field_name: str | None = None,
) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (Decimal, int, float)):
        try:
            return {"$number": _normalized_number(value, policy.numeric_tolerance)}
        except (InvalidOperation, ValueError):
            return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        if field_name in {"symbol", "code", "ts_code"}:
            return text.split(".")[0][-6:].zfill(6)
        try:
            if field_name and any(
                token in field_name.lower() for token in ("date", "time", "_at")
            ):
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.isoformat(timespec="seconds")
                return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (AttributeError, ValueError):
            pass
        return text
    if isinstance(value, list):
        rows = [_normalize(item, policy=policy) for item in value]
        return rows
    if isinstance(value, dict):
        projected = value
        if policy.business_fields and not isinstance(value.get("rows"), list):
            matching = {
                key: child
                for key, child in value.items()
                if key in policy.business_fields
            }
            if matching:
                projected = matching
        return {
            key: _normalize(child, policy=policy, field_name=key)
            for key, child in sorted(projected.items())
            if key not in _METADATA_KEYS
        }
    return str(value)


def canonical_value(value: Any, policy: CapabilityQualityPolicy) -> Any:
    if policy.capability == "fundamental.profile" and isinstance(value, dict):
        aliases = {
            "name": ("name", "A股简称", "公司名称"),
            "industry": ("industry", "所属行业", "细分行业"),
            "market": ("market", "所属市场"),
            "main_business": ("main_business", "主营业务"),
            "business_scope": ("business_scope", "经营范围"),
            "website": ("website", "官方网站"),
        }
        value = {
            target: next(
                (
                    value[key]
                    for key in keys
                    if value.get(key) not in (None, "")
                ),
                None,
            )
            for target, keys in aliases.items()
        }
    if policy.capability == "announcement.catalog" and isinstance(value, list):
        rows = []
        for item in value:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "title": next(
                        (
                            item[key]
                            for key in ("title", "公告标题", "公告名称")
                            if item.get(key)
                        ),
                        None,
                    ),
                    "published_date": next(
                        (
                            item[key]
                            for key in ("published_date", "公告时间", "公告日期", "date")
                            if item.get(key)
                        ),
                        None,
                    ),
                    "url": next(
                        (
                            item[key]
                            for key in ("url", "公告链接", "网址")
                            if item.get(key)
                        ),
                        None,
                    ),
                }
            )
        value = sorted(rows, key=lambda row: (str(row["published_date"]), str(row["title"])))
    if policy.capability == "market.daily" and isinstance(value, list):
        rows = []
        for item in value[-5:]:
            row = asdict(item) if is_dataclass(item) else dict(item)
            source = str(row.get("source") or "").lower()
            row["adjustment"] = (
                "qfq"
                if "qfq" in source
                else "hfq"
                if "hfq" in source
                else "unadjusted"
            )
            row["price_unit"] = "CNY"
            row["volume_unit"] = "share"
            rows.append(row)
        value = rows
    if isinstance(value, list) and policy.capability == "market.daily":
        value = value[-5:]
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        value = {"rows": value["rows"][-5:]}
    return _normalize(value, policy=policy)


def canonical_digest(value: Any, policy: CapabilityQualityPolicy) -> str:
    encoded = json.dumps(
        canonical_value(value, policy),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def observation_is_stale(
    observed_at: datetime | date | None,
    policy: CapabilityQualityPolicy,
    *,
    now: datetime | None = None,
    calendar: TradingCalendar | None = None,
) -> bool:
    if observed_at is None:
        return True
    current = now or datetime.now()
    if policy.max_trading_session_lag is not None:
        observed_date = (
            observed_at.date() if isinstance(observed_at, datetime) else observed_at
        )
        return (
            (calendar or get_trading_calendar()).session_lag(observed_date, current)
            > policy.max_trading_session_lag
        )
    if policy.max_age is None:
        return False
    observed_dt = (
        observed_at
        if isinstance(observed_at, datetime)
        else datetime.combine(observed_at, datetime.min.time())
    )
    if observed_dt.tzinfo and current.tzinfo is None:
        current = current.replace(tzinfo=observed_dt.tzinfo)
    elif current.tzinfo and observed_dt.tzinfo is None:
        observed_dt = observed_dt.replace(tzinfo=current.tzinfo)
    return current - observed_dt > policy.max_age


def assess_quality(
    observations: list[QualityObservation],
    policy: CapabilityQualityPolicy | None = None,
) -> DataQualityStatus:
    policy = policy or DEFAULT_POLICY
    usable = [item for item in observations if item.value is not None]
    if not usable:
        return DataQualityStatus.MISSING
    fresh = [item for item in usable if not item.stale]
    if not fresh:
        return DataQualityStatus.STALE
    if len(fresh) == 1:
        return DataQualityStatus.SINGLE_SOURCE
    digests = {
        item.normalized_digest or canonical_digest(item.value, policy)
        for item in fresh
    }
    return (
        DataQualityStatus.VERIFIED
        if len(digests) == 1
        else DataQualityStatus.CONFLICTED
    )


__all__ = [
    "CapabilityQualityPolicy",
    "DataQualityStatus",
    "QualityObservation",
    "assess_quality",
    "canonical_digest",
    "canonical_value",
    "observation_is_stale",
    "policy_for",
    "worst_quality",
]
