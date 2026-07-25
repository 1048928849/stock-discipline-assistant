from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.research_subjects import (
    announcement_catalog_subject,
    company_profile_subject,
)
from app.data_hub.router import DataHubRouter, ProviderResult
from app.domain.quality_subject import EffectiveQualityResult, SubjectRef
from app.models import (
    CompanyAnnouncement,
    CompanyProfile,
    CompanyResearchRefresh,
    DataQualityRecord,
)


PROFILE_CAPABILITY = "fundamental.profile"
ANNOUNCEMENT_CAPABILITY = "announcement.catalog"


@dataclass(frozen=True)
class CachedCompanyProfileSelection:
    profile: CompanyProfile | None
    subject: SubjectRef
    quality_record_id: int | None
    effective_quality: EffectiveQualityResult
    observed_at: datetime | None
    executable: bool
    blocking_reason: str | None
    structure_reason: str | None = None


@dataclass(frozen=True)
class CachedAnnouncementCatalogSelection:
    announcements: list[CompanyAnnouncement]
    refresh: CompanyResearchRefresh | None
    subject: SubjectRef
    quality_record_id: int | None
    effective_quality: EffectiveQualityResult
    checked_at: datetime | None
    scan_start: datetime | None
    scan_end: datetime | None
    executable: bool
    blocking_reason: str | None
    structure_reason: str | None = None


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _json_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _require_lineage(
    router: DataHubRouter,
    result: ProviderResult,
    *,
    capability: str,
    subject: SubjectRef,
) -> DataQualityRecord:
    if result.capability != capability:
        raise ProviderUnavailableError("research result capability does not match cache")
    if result.subject != subject:
        raise ProviderUnavailableError("research result subject does not match cache")
    result.require_trusted_value()
    return router.validate_persistence_result(result)


def persist_company_profile(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
) -> CompanyProfile:
    payload = result.value
    if not isinstance(payload, dict):
        raise ProviderUnavailableError("company profile payload must be a mapping")
    subject = result.subject
    if subject is None:
        raise ProviderUnavailableError("company profile result has no subject")
    expected = company_profile_subject(subject.subject_id)
    record = _require_lineage(
        router, result, capability=PROFILE_CAPABILITY, subject=expected
    )
    if _naive_utc(record.observed_at) != _naive_utc(result.fetched_at):
        raise ProviderUnavailableError("company profile lineage time does not match fetch")

    symbol = expected.subject_id
    profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
    values = {
        "name": str(
            payload.get("A股简称")
            or payload.get("公司名称")
            or payload.get("name")
            or symbol
        ),
        "industry": payload.get("细分行业")
        or payload.get("所属行业")
        or payload.get("industry"),
        "market": payload.get("所属市场") or payload.get("market"),
        "main_business": payload.get("主营业务") or payload.get("main_business"),
        "business_scope": payload.get("经营范围") or payload.get("business_scope"),
        "website": payload.get("官方网站") or payload.get("website"),
        "source": result.provider_id,
        "source_url": None,
        "raw_data": _json_value(payload),
        "fetched_at": _naive_utc(result.fetched_at),
        "quality_record_id": record.id,
    }
    if profile is None:
        profile = CompanyProfile(symbol=symbol, **values)
        db.add(profile)
    else:
        for key, value in values.items():
            setattr(profile, key, value)
    db.flush()
    router.mark_persisted(result, cached_at=result.fetched_at)
    return profile


def persist_announcement_catalog(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
    *,
    start: date,
    end: date,
) -> CompanyResearchRefresh:
    if not isinstance(result.value, list):
        raise ProviderUnavailableError("announcement catalog payload must be a list")
    subject = announcement_catalog_subject(
        result.subject.subject_id if result.subject else "", start, end
    )
    record = _require_lineage(
        router, result, capability=ANNOUNCEMENT_CAPABILITY, subject=subject
    )
    expected_start = datetime.combine(start, time.min)
    expected_end = datetime.combine(end, time.max)
    if (
        _naive_utc(result.scan_start) != expected_start
        or _naive_utc(result.scan_end) != expected_end
        or _naive_utc(record.scan_start) != expected_start
        or _naive_utc(record.scan_end) != expected_end
        or result.checked_at is None
        or _naive_utc(record.checked_at) != _naive_utc(result.checked_at)
        or _naive_utc(record.observed_at) != _naive_utc(result.checked_at)
    ):
        raise ProviderUnavailableError("announcement catalog scan lineage is inconsistent")

    symbol = subject.subject_id
    db.execute(
        delete(CompanyAnnouncement).where(
            CompanyAnnouncement.symbol == symbol,
            CompanyAnnouncement.published_date >= start,
            CompanyAnnouncement.published_date <= end,
        )
    )
    from app.services.company_research import _announcement_fields, classify_announcement

    for row in result.value:
        if not isinstance(row, dict):
            raise ProviderUnavailableError("announcement catalog row must be a mapping")
        title, published, url, catalog = _announcement_fields(row)
        if not url:
            continue
        category, risk = classify_announcement(title)
        db.add(
            CompanyAnnouncement(
                symbol=symbol,
                title=title,
                announcement_category=category,
                risk_level=risk,
                published_date=published,
                catalog_source=catalog or result.provider_id,
                exchange="上交所" if symbol.startswith(("5", "6", "9")) else "深交所",
                url=url,
                source_document_url=None,
                raw_data=_json_value(row),
                fetched_at=_naive_utc(result.fetched_at),
            )
        )

    refresh = db.scalar(
        select(CompanyResearchRefresh).where(
            CompanyResearchRefresh.symbol == symbol,
            CompanyResearchRefresh.section == "announcements",
        )
    )
    checked_at = _naive_utc(result.checked_at)
    values = {
        "status": "success",
        "provider_id": result.provider_id,
        "source_name": result.provider_id,
        "row_count": len(result.value),
        "cache_used": False,
        "last_attempt_at": checked_at,
        "last_success_at": checked_at,
        "data_date": end,
        "stale_after": None,
        "quality_status": result.quality_status.value,
        "observed_at": checked_at,
        "fetched_at": _naive_utc(result.fetched_at),
        "checked_at": checked_at,
        "scan_start": expected_start,
        "scan_end": expected_end,
        "normalized_digest": result.normalized_digest,
        "provider_observations": result.provider_observations,
        "conflict_fields": result.conflict_fields,
        "error": None,
        "quality_record_id": record.id,
    }
    if refresh is None:
        refresh = CompanyResearchRefresh(
            symbol=symbol, section="announcements", **values
        )
        db.add(refresh)
    else:
        for key, value in values.items():
            setattr(refresh, key, value)
    db.flush()
    router.mark_persisted(result, cached_at=result.fetched_at)
    return refresh


def resolve_cached_company_profile(
    db: Session,
    symbol: str,
    *,
    evaluated_at: datetime | None = None,
) -> CachedCompanyProfileSelection:
    subject = company_profile_subject(symbol)
    profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == subject.subject_id))
    record_id = profile.quality_record_id if profile else None
    observed_at = profile.fetched_at if profile else None
    effective = resolve_effective_quality(
        db,
        capability=PROFILE_CAPABILITY,
        subject=subject,
        persisted_quality_record_id=record_id,
        observed_at=observed_at,
        evaluated_at=evaluated_at,
    )
    structure_reason = None
    record = db.get(DataQualityRecord, record_id) if record_id else None
    if profile is None:
        structure_reason = "company_profile_missing"
    elif record_id is None:
        structure_reason = "legacy_missing_quality_lineage"
    elif record is None:
        structure_reason = "quality_lineage_missing"
    elif (
        record.capability != PROFILE_CAPABILITY
        or record.subject_type != subject.subject_type
        or record.subject_id != subject.subject_id
        or (record.semantic_key or "").strip() != subject.semantic_key
        or not record.persisted
        or record.provider_id != profile.source
        or _naive_utc(record.observed_at) != _naive_utc(profile.fetched_at)
    ):
        structure_reason = "company_profile_lineage_mismatch"
    executable = structure_reason is None and effective.executable
    return CachedCompanyProfileSelection(
        profile=profile if executable else None,
        subject=subject,
        quality_record_id=record_id,
        effective_quality=effective,
        observed_at=observed_at,
        executable=executable,
        blocking_reason=structure_reason or effective.blocking_reason,
        structure_reason=structure_reason,
    )


def resolve_cached_announcement_catalog(
    db: Session,
    symbol: str,
    start: date,
    end: date,
    *,
    evaluated_at: datetime | None = None,
) -> CachedAnnouncementCatalogSelection:
    subject = announcement_catalog_subject(symbol, start, end)
    refresh = db.scalar(
        select(CompanyResearchRefresh).where(
            CompanyResearchRefresh.symbol == subject.subject_id,
            CompanyResearchRefresh.section == "announcements",
        )
    )
    record_id = refresh.quality_record_id if refresh else None
    checked_at = refresh.checked_at if refresh else None
    effective = resolve_effective_quality(
        db,
        capability=ANNOUNCEMENT_CAPABILITY,
        subject=subject,
        persisted_quality_record_id=record_id,
        observed_at=checked_at,
        evaluated_at=evaluated_at,
    )
    expected_start = datetime.combine(start, time.min)
    expected_end = datetime.combine(end, time.max)
    structure_reason = None
    record = db.get(DataQualityRecord, record_id) if record_id else None
    if refresh is None:
        structure_reason = "announcement_refresh_missing"
    elif record_id is None:
        structure_reason = "legacy_missing_quality_lineage"
    elif record is None:
        structure_reason = "quality_lineage_missing"
    elif (
        record.capability != ANNOUNCEMENT_CAPABILITY
        or record.subject_type != subject.subject_type
        or record.subject_id != subject.subject_id
        or (record.semantic_key or "").strip() != subject.semantic_key
        or not record.persisted
        or refresh.provider_id != record.provider_id
        or refresh.normalized_digest != record.normalized_digest
        or _naive_utc(refresh.scan_start) != expected_start
        or _naive_utc(refresh.scan_end) != expected_end
        or _naive_utc(record.scan_start) != expected_start
        or _naive_utc(record.scan_end) != expected_end
        or _naive_utc(record.checked_at) != _naive_utc(refresh.checked_at)
        or _naive_utc(record.observed_at) != _naive_utc(refresh.checked_at)
    ):
        structure_reason = "announcement_catalog_lineage_mismatch"
    executable = structure_reason is None and effective.executable
    announcements = (
        list(
            db.scalars(
                select(CompanyAnnouncement)
                .where(
                    CompanyAnnouncement.symbol == subject.subject_id,
                    CompanyAnnouncement.published_date >= start,
                    CompanyAnnouncement.published_date <= end,
                )
                .order_by(CompanyAnnouncement.published_date.desc(), CompanyAnnouncement.id)
            ).all()
        )
        if executable
        else []
    )
    return CachedAnnouncementCatalogSelection(
        announcements=announcements,
        refresh=refresh,
        subject=subject,
        quality_record_id=record_id,
        effective_quality=effective,
        checked_at=checked_at,
        scan_start=refresh.scan_start if refresh else None,
        scan_end=refresh.scan_end if refresh else None,
        executable=executable,
        blocking_reason=structure_reason or effective.blocking_reason,
        structure_reason=structure_reason,
    )


__all__ = [
    "CachedAnnouncementCatalogSelection",
    "CachedCompanyProfileSelection",
    "persist_announcement_catalog",
    "persist_company_profile",
    "resolve_cached_announcement_catalog",
    "resolve_cached_company_profile",
]
