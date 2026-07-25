from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.effective_quality import resolve_effective_quality
from app.data_hub.quality import canonical_digest, policy_for
from app.data_hub.research_subjects import (
    announcement_catalog_subject,
    company_profile_subject,
)
from app.data_hub.router import DataHubRouter, ProviderResult
from app.data_hub.trading_calendar import to_utc_storage_naive
from app.domain.quality_subject import EffectiveQualityResult, SubjectRef
from app.models import (
    CompanyAnnouncement,
    CompanyProfile,
    CompanyResearchRefresh,
    DataQualityRecord,
)
from app.services.url_normalization import normalize_announcement_url


PROFILE_CAPABILITY = "fundamental.profile"
ANNOUNCEMENT_CAPABILITY = "announcement.catalog"
PAYLOAD_LINEAGE_ERROR = (
    "research persistence payload does not match quality lineage"
)


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


@dataclass(frozen=True)
class _PreparedAnnouncementRow:
    symbol: str
    title: str
    announcement_category: str
    risk_level: str
    published_date: date
    catalog_source: str
    exchange: str
    url: str
    source_document_url: str | None
    raw_data: dict
    fetched_at: datetime


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return to_utc_storage_naive(value, naive_is_utc=value.tzinfo is None)


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


def _validated_payload_snapshot(
    router: DataHubRouter,
    result: ProviderResult,
    *,
    capability: str,
    subject: SubjectRef,
) -> tuple[dict | list, DataQualityRecord]:
    try:
        snapshot = deepcopy(result.value)
        if capability == PROFILE_CAPABILITY:
            if not isinstance(snapshot, dict) or not snapshot:
                raise ValueError("profile payload must be a non-empty mapping")
        else:
            if not isinstance(snapshot, list):
                raise ValueError("announcement payload must be a list")
        expected_row_count = router.payload_row_count(capability, snapshot)
        record = _require_lineage(
            router,
            result,
            capability=capability,
            subject=subject,
        )
        recomputed_digest = canonical_digest(snapshot, policy_for(capability))
        if (
            recomputed_digest != result.normalized_digest
            or recomputed_digest != record.normalized_digest
            or record.row_count != expected_row_count
        ):
            raise ValueError("payload digest or row count mismatch")
        return snapshot, record
    except (ProviderUnavailableError, TypeError, ValueError) as exc:
        raise ProviderUnavailableError(PAYLOAD_LINEAGE_ERROR) from exc


def _preflight_announcements(
    snapshot: list,
    *,
    symbol: str,
    start: date,
    end: date,
    fetched_at: datetime,
) -> list[_PreparedAnnouncementRow]:
    from app.services.company_research import _announcement_fields, classify_announcement

    prepared: list[_PreparedAnnouncementRow] = []
    urls: set[str] = set()
    for row in snapshot:
        if not isinstance(row, dict):
            raise ProviderUnavailableError("announcement catalog row must be a mapping")
        raw_title = row.get("公告标题") or row.get("标题") or row.get("title")
        if not isinstance(raw_title, str) or not raw_title.strip():
            raise ProviderUnavailableError("announcement catalog row has no title")
        try:
            title, published, url, catalog = _announcement_fields(row)
        except Exception as exc:
            raise ProviderUnavailableError(
                "announcement catalog row cannot be parsed"
            ) from exc
        title = title.strip()
        url = normalize_announcement_url(url)
        catalog = catalog.strip()
        if not title:
            raise ProviderUnavailableError("announcement catalog row has no title")
        try:
            in_requested_coverage = (
                isinstance(published, date) and start <= published <= end
            )
        except (TypeError, ValueError):
            in_requested_coverage = False
        if not in_requested_coverage:
            raise ProviderUnavailableError(
                "announcement catalog row is outside requested coverage"
            )
        if url in urls:
            raise ProviderUnavailableError("announcement catalog contains duplicate URLs")
        urls.add(url)
        category, risk = classify_announcement(title)
        prepared.append(
            _PreparedAnnouncementRow(
                symbol=symbol,
                title=title,
                announcement_category=category,
                risk_level=risk,
                published_date=published,
                catalog_source=catalog,
                exchange="上交所" if symbol.startswith(("5", "6", "9")) else "深交所",
                url=url,
                source_document_url=None,
                raw_data=_json_value(row),
                fetched_at=fetched_at,
            )
        )
    if len(prepared) != len(snapshot):
        raise ProviderUnavailableError(
            "prepared announcement row count does not match provider payload"
        )
    return prepared


def persist_company_profile(
    db: Session,
    router: DataHubRouter,
    result: ProviderResult,
) -> CompanyProfile:
    subject = result.subject
    if subject is None:
        raise ProviderUnavailableError("company profile result has no subject")
    expected = company_profile_subject(subject.subject_id)
    payload, record = _validated_payload_snapshot(
        router,
        result,
        capability=PROFILE_CAPABILITY,
        subject=expected,
    )
    if _naive_utc(record.observed_at) != _naive_utc(result.fetched_at):
        raise ProviderUnavailableError("company profile lineage time does not match fetch")

    symbol = expected.subject_id
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
    with db.begin_nested():
        profile = db.scalar(
            select(CompanyProfile).where(CompanyProfile.symbol == symbol)
        )
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
    subject = announcement_catalog_subject(
        result.subject.subject_id if result.subject else "", start, end
    )
    snapshot, record = _validated_payload_snapshot(
        router,
        result,
        capability=ANNOUNCEMENT_CAPABILITY,
        subject=subject,
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
    prepared = _preflight_announcements(
        snapshot,
        symbol=symbol,
        start=start,
        end=end,
        fetched_at=_naive_utc(result.fetched_at),
    )
    checked_at = _naive_utc(result.checked_at)
    values = {
        "status": "success",
        "provider_id": result.provider_id,
        "source_name": result.provider_id,
        "row_count": len(snapshot),
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
    with db.begin_nested():
        refresh = db.scalar(
            select(CompanyResearchRefresh).where(
                CompanyResearchRefresh.symbol == symbol,
                CompanyResearchRefresh.section == "announcements",
            )
        )
        db.execute(
            delete(CompanyAnnouncement).where(
                CompanyAnnouncement.symbol == symbol,
                CompanyAnnouncement.published_date >= start,
                CompanyAnnouncement.published_date <= end,
            )
        )
        db.add_all([CompanyAnnouncement(**asdict(row)) for row in prepared])
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
