from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import sqlite
from sqlalchemy.exc import IntegrityError

from app.data_hub.contracts import DataProvider, ProviderMetadata, ProviderUnavailableError
from app.data_hub.registry import ProviderRegistry
from app.data_hub.research_subjects import (
    announcement_catalog_window,
    announcement_catalog_subject,
    company_profile_subject,
)
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import (
    SHANGHAI_TZ,
    market_storage_naive_to_aware,
    utc_storage_naive_to_aware,
)
from app.models import (
    CompanyAnnouncement,
    CompanyProfile,
    CompanyResearchRefresh,
    DataQualityRecord,
)
from app.domain.models import Evidence, SourceQualityBinding
from app.domain.quality import DataQualityStatus
from app.services.research_cache import (
    persist_announcement_catalog,
    persist_company_profile,
    resolve_cached_announcement_catalog,
    resolve_cached_company_profile,
)
from app.services.company_research import sync_company_research
from app.services.one_click_pipeline import _research_inventory
from app.services.url_normalization import normalize_announcement_url


class ResearchProvider(DataProvider):
    metadata = ProviderMetadata(
        provider_id="research-test",
        supported_capabilities=("fundamental.profile", "announcement.catalog"),
        priority=1,
    )

    def health_check(self, probe: bool = False):
        return {"status": "healthy"}

    def company_profile(self, symbol):
        return {"A股简称": "测试公司", "细分行业": "测试行业"}

    def company_announcements(self, symbol, start, end):
        return []


class ScenarioResearchProvider(ResearchProvider):
    def __init__(
        self,
        provider_id,
        *,
        profile=None,
        announcements=None,
        fail_profile=False,
        fail_announcements=False,
        priority=1,
    ):
        self.metadata = ProviderMetadata(
            provider_id=provider_id,
            supported_capabilities=("fundamental.profile", "announcement.catalog"),
            priority=priority,
        )
        self.profile = profile
        self.announcements = announcements
        self.fail_profile = fail_profile
        self.fail_announcements = fail_announcements

    def company_profile(self, symbol):
        if self.fail_profile:
            raise ProviderUnavailableError("profile unavailable")
        return self.profile or super().company_profile(symbol)

    def company_announcements(self, symbol, start, end):
        if self.fail_announcements:
            raise ProviderUnavailableError("catalog unavailable")
        return self.announcements if self.announcements is not None else []


def _scenario_router(session, *providers):
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return DataHubRouter(session, registry)


def _router(session):
    registry = ProviderRegistry()
    registry.register(ResearchProvider())
    return DataHubRouter(session, registry)


def _router_at(session, now, provider=None):
    registry = ProviderRegistry()
    registry.register(provider or ResearchProvider())
    return DataHubRouter(session, registry, now_fn=lambda: now)


class MutatingProfileRouter(DataHubRouter):
    def __init__(self, session, registry, mutation):
        super().__init__(session, registry)
        self.mutation = mutation
        self.profile_result = None

    def company_profile(self, symbol):
        result = super().company_profile(symbol)
        self.profile_result = result
        self.mutation(self, result)
        return result


def _mutating_profile_router(session, mutation, *, profile=None):
    registry = ProviderRegistry()
    registry.register(
        ScenarioResearchProvider(
            "mutating-profile",
            profile=profile
            or {"name": "replacement", "industry": "replacement-industry"},
        )
    )
    return MutatingProfileRouter(session, registry, mutation)


def _announcement_row(index, published, *, url=None, title=None):
    return {
        "公告标题": title if title is not None else f"公告 {index}",
        "公告日期": published.isoformat(),
        "公告链接": url or f"https://example.test/announcement-{index}",
        "目录来源": "test-catalog",
    }


def _catalog_router(session, rows, provider_id="catalog-test"):
    return _scenario_router(
        session,
        ScenarioResearchProvider(provider_id, announcements=rows),
    )


def _persist_catalog(session, rows, start, end, provider_id="catalog-test"):
    router = _catalog_router(session, rows, provider_id)
    result = router.company_announcements("300502", start, end)
    refresh = persist_announcement_catalog(
        session, router, result, start=start, end=end
    )
    return router, result, refresh


def test_announcement_url_normalization_preserves_valid_ascii_url():
    url = "https://example.test/path/to/report?q=1&lang=en#section"
    assert normalize_announcement_url(f"  {url}  ") == url


def test_sqlite_announcement_url_remains_varchar_1000():
    compiled = CompanyAnnouncement.__table__.c.url.type.compile(
        dialect=sqlite.dialect()
    )
    assert compiled == "VARCHAR(1000)"


def test_announcement_url_normalization_encodes_idna_and_unicode_components():
    normalized = normalize_announcement_url(
        "https://例子.测试/公告 路径?q=你好#片段"
    )
    assert normalized.startswith("https://xn--fsqu00a.xn--0zwm56d/")
    assert "%E5%85%AC%E5%91%8A%20%E8%B7%AF%E5%BE%84" in normalized
    assert "q=%E4%BD%A0%E5%A5%BD" in normalized
    assert normalized.isascii()


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.test/report",
        "https:///missing-host",
        "https://example.test:invalid/report",
        "https://example.test:0/report",
        "https://example.test/report\nheader",
    ],
)
def test_invalid_announcement_urls_are_rejected(url):
    with pytest.raises(ProviderUnavailableError, match="invalid announcement URL"):
        normalize_announcement_url(url)


def test_announcement_url_length_boundary():
    prefix = "https://example.test/"
    accepted = prefix + "a" * (1000 - len(prefix))
    assert len(normalize_announcement_url(accepted)) == 1000
    with pytest.raises(ProviderUnavailableError, match="exceeds 1000"):
        normalize_announcement_url(accepted + "a")


def test_unicode_announcement_url_preserves_raw_payload_and_digest(session):
    start, end = date.today() - timedelta(days=30), date.today()
    raw_url = "https://例子.测试/公告?q=你好"
    row = _announcement_row(1, end, url=raw_url)
    router = _catalog_router(session, [row])
    result = router.company_announcements("300502", start, end)
    original_digest = result.normalized_digest
    refresh = persist_announcement_catalog(
        session, router, result, start=start, end=end
    )
    stored = session.query(CompanyAnnouncement).one()
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert refresh.row_count == 1
    assert stored.url == normalize_announcement_url(raw_url)
    assert stored.raw_data["公告链接"] == raw_url
    assert result.value[0]["公告链接"] == raw_url
    assert result.normalized_digest == original_digest == record.normalized_digest


def test_normalized_duplicate_urls_are_rejected_before_delete(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _seed_complete_catalog(session, start, end, count=2)
    old_rows = [item.url for item in session.query(CompanyAnnouncement).order_by(CompanyAnnouncement.id)]
    rows = [
        _announcement_row(10, end, url="https://例子.测试/公告"),
        _announcement_row(11, end, url="https://xn--fsqu00a.xn--0zwm56d/%E5%85%AC%E5%91%8A"),
    ]
    router = _catalog_router(session, rows, "normalized-duplicate")
    result = router.company_announcements("300502", start, end)
    with pytest.raises(ProviderUnavailableError, match="duplicate URLs"):
        persist_announcement_catalog(session, router, result, start=start, end=end)
    session.commit()
    assert [
        item.url for item in session.query(CompanyAnnouncement).order_by(CompanyAnnouncement.id)
    ] == old_rows
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False


def test_company_profile_subject_is_stable():
    assert company_profile_subject(" 300502 ") == company_profile_subject("300502")
    assert company_profile_subject("300502").model_dump() == {
        "subject_type": "stock",
        "subject_id": "300502",
        "semantic_key": "profile",
    }


def test_announcement_subject_includes_exact_coverage():
    subject = announcement_catalog_subject(
        "300502", date(2025, 7, 25), date(2026, 7, 25)
    )
    assert subject.semantic_key == "catalog/2025-07-25/2026-07-25"


def test_different_announcement_windows_are_isolated():
    first = announcement_catalog_subject(
        "300502", date(2025, 7, 25), date(2026, 7, 25)
    )
    second = announcement_catalog_subject(
        "300502", date(2025, 7, 26), date(2026, 7, 25)
    )
    assert first != second
    with pytest.raises(ValueError):
        announcement_catalog_subject(
            "300502", date(2026, 7, 26), date(2026, 7, 25)
        )


def test_announcement_catalog_window_uses_shanghai_business_date():
    evaluated_at = datetime(2026, 7, 25, 9, 30, tzinfo=SHANGHAI_TZ)
    start, end = announcement_catalog_window(evaluated_at=evaluated_at)
    assert end == date(2026, 7, 25)
    assert start == end - timedelta(days=3 * 366)


def test_announcement_catalog_window_converts_utc_instant_to_shanghai_date():
    start, end = announcement_catalog_window(
        evaluated_at=datetime(2026, 7, 24, 16, 30, tzinfo=timezone.utc)
    )
    assert end == date(2026, 7, 25)
    assert start == date(2023, 7, 23)


def test_announcement_catalog_window_rejects_naive_evaluation_time():
    with pytest.raises(ValueError, match="naive datetime"):
        announcement_catalog_window(evaluated_at=datetime(2026, 7, 25, 9, 30))


def test_announcement_catalog_window_does_not_read_host_timezone(monkeypatch):
    evaluated_at = datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    first = announcement_catalog_window(evaluated_at=evaluated_at)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    second = announcement_catalog_window(evaluated_at=evaluated_at)
    assert first == second
    assert first[1] == date(2026, 7, 25)


def test_different_business_days_create_different_announcement_subjects():
    first_window = announcement_catalog_window(
        evaluated_at=datetime(2026, 7, 25, 15, 59, tzinfo=timezone.utc)
    )
    second_window = announcement_catalog_window(
        evaluated_at=datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc)
    )
    assert announcement_catalog_subject("300502", *first_window) != (
        announcement_catalog_subject("300502", *second_window)
    )


def test_sync_and_inventory_reuse_exact_announcement_window_across_midnight(
    session, monkeypatch
):
    analysis_started_at = datetime(2026, 7, 25, 23, 59, tzinfo=SHANGHAI_TZ)
    next_day = analysis_started_at + timedelta(minutes=2)
    router = _router_at(session, analysis_started_at)
    sync_company_research(
        session,
        "300502",
        provider=router,
        include_documents=False,
        evaluated_at=analysis_started_at,
    )
    monkeypatch.setattr(
        "app.services.one_click_pipeline.shanghai_now", lambda: next_day
    )
    _, step = _research_inventory(
        session, "300502", evaluated_at=analysis_started_at
    )
    expected_start, expected_end = announcement_catalog_window(
        evaluated_at=analysis_started_at
    )
    binding = step["source_quality_binding"]
    assert binding["semantic_key"] == (
        f"catalog/{expected_start.isoformat()}/{expected_end.isoformat()}"
    )
    assert datetime.fromisoformat(step["data_time"]) == analysis_started_at
    assert datetime.fromisoformat(step["observed_at"]).tzinfo is not None
    assert all(
        datetime.fromisoformat(binding[field]).tzinfo is None
        for field in ("observed_at", "scan_start", "scan_end", "checked_at")
    )
    assert datetime.fromisoformat(step["observed_at"]).astimezone(
        timezone.utc
    ).replace(tzinfo=None) == datetime.fromisoformat(binding["observed_at"])


def test_profile_router_rejects_missing_subject(session):
    with pytest.raises(ProviderUnavailableError, match="requires an explicit research subject"):
        _router(session).invoke(
            "fundamental.profile", "company_profile", "300502", symbol="300502"
        )


def test_announcement_router_rejects_missing_subject(session):
    with pytest.raises(ProviderUnavailableError, match="requires an explicit research subject"):
        _router(session).invoke(
            "announcement.catalog",
            "company_announcements",
            "300502",
            date(2025, 7, 25),
            date(2026, 7, 25),
            symbol="300502",
        )


def test_profile_quality_record_has_stock_subject(session):
    result = _router(session).company_profile("300502")
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert result.subject == company_profile_subject("300502")
    assert (record.subject_type, record.subject_id, record.semantic_key) == (
        "stock",
        "300502",
        "profile",
    )


def test_announcement_record_contains_scan_metadata(session):
    start = date(2025, 7, 25)
    end = date(2026, 7, 25)
    result = _router(session).company_announcements("300502", start, end)
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert result.subject == announcement_catalog_subject("300502", start, end)
    assert record.scan_start == datetime.combine(start, datetime.min.time())
    assert record.scan_end == datetime.combine(end, datetime.max.time())
    assert record.checked_at == record.observed_at
    assert record.latest_content_at is None
    assert record.row_count == 0
    assert result.quality_status.value == "SINGLE_SOURCE"


def test_research_cache_models_expose_nullable_quality_lineage(session):
    profile = CompanyProfile(
        symbol="300502",
        name="测试公司",
        industry=None,
        market=None,
        main_business=None,
        business_scope=None,
        website=None,
        source="legacy",
        source_url=None,
        raw_data=None,
        fetched_at=datetime.now(),
        quality_record_id=None,
    )
    refresh = CompanyResearchRefresh(
        symbol="300502",
        section="announcements",
        status="legacy",
        row_count=0,
        cache_used=False,
        last_attempt_at=datetime.now(),
        quality_record_id=None,
    )
    session.add_all([profile, refresh])
    session.flush()
    assert profile.quality_record_id is None
    assert refresh.quality_record_id is None


def test_profile_persistence_binds_exact_quality_record(session):
    router = _router(session)
    result = router.company_profile("300502")
    profile = persist_company_profile(session, router, result)
    selected = resolve_cached_company_profile(session, "300502")
    assert profile.quality_record_id == result.quality_record_id
    assert selected.profile is profile
    assert selected.quality_record_id == result.quality_record_id
    assert selected.executable is True
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True


def test_successful_empty_scan_is_executable(session):
    start = date.today() - timedelta(days=30)
    end = date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    refresh = persist_announcement_catalog(
        session, router, result, start=start, end=end
    )
    selected = resolve_cached_announcement_catalog(
        session, "300502", start, end
    )
    assert refresh.row_count == 0
    assert refresh.quality_record_id == result.quality_record_id
    assert selected.announcements == []
    assert selected.executable is True


def test_profile_legacy_without_lineage_is_missing(session):
    profile = CompanyProfile(
        symbol="300502",
        name="legacy",
        industry="legacy",
        market=None,
        main_business=None,
        business_scope=None,
        website=None,
        source="legacy",
        source_url=None,
        raw_data=None,
        fetched_at=datetime.now(),
    )
    session.add(profile)
    session.flush()
    selected = resolve_cached_company_profile(session, "300502")
    assert selected.profile is None
    assert selected.effective_quality.effective_quality.value == "MISSING"
    assert selected.executable is False
    assert selected.structure_reason == "legacy_missing_quality_lineage"


def test_profile_selector_dynamically_ages(session):
    router = _router(session)
    result = router.company_profile("300502")
    persist_company_profile(session, router, result)
    selected = resolve_cached_company_profile(
        session,
        "300502",
        evaluated_at=result.observed_at + timedelta(days=31),
    )
    assert selected.effective_quality.effective_quality.value == "STALE"
    assert selected.executable is False


def _persist_profile_at(session, acquired_at):
    router = _router_at(session, acquired_at)
    result = router.company_profile("300502")
    profile = persist_company_profile(session, router, result)
    session.commit()
    return result, profile


def _persist_catalog_at(session, acquired_at):
    start = acquired_at.date() - timedelta(days=30)
    end = acquired_at.date()
    router = _router_at(session, acquired_at)
    result = router.company_announcements("300502", start, end)
    refresh = persist_announcement_catalog(
        session,
        router,
        result,
        start=start,
        end=end,
    )
    session.commit()
    return start, end, result, refresh


def test_profile_utc_naive_storage_does_not_age_eight_hours_early(session):
    acquired_at = datetime(2026, 6, 1, 10, 0, tzinfo=SHANGHAI_TZ)
    result, profile = _persist_profile_at(session, acquired_at)
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert profile.fetched_at == datetime(2026, 6, 1, 2, 0)
    assert record.observed_at == datetime(2026, 6, 1, 2, 0)
    selected = resolve_cached_company_profile(
        session,
        "300502",
        evaluated_at=acquired_at + timedelta(days=29, hours=23, minutes=59),
    )
    assert selected.executable is True
    assert selected.effective_quality.effective_quality == DataQualityStatus.SINGLE_SOURCE


def test_profile_stales_only_after_exact_30_days(session):
    acquired_at = datetime(2026, 6, 1, 10, 0, tzinfo=SHANGHAI_TZ)
    _persist_profile_at(session, acquired_at)
    at_boundary = resolve_cached_company_profile(
        session,
        "300502",
        evaluated_at=acquired_at + timedelta(days=30),
    )
    after_boundary = resolve_cached_company_profile(
        session,
        "300502",
        evaluated_at=acquired_at + timedelta(days=30, seconds=1),
    )
    assert at_boundary.executable is True
    assert after_boundary.effective_quality.effective_quality == DataQualityStatus.STALE
    assert after_boundary.executable is False


def test_announcement_utc_naive_storage_remains_fresh_for_24_hours(session):
    acquired_at = datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    start, end, result, refresh = _persist_catalog_at(session, acquired_at)
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert refresh.checked_at == datetime(2026, 7, 24, 2, 0)
    assert record.observed_at == datetime(2026, 7, 24, 2, 0)
    selected = resolve_cached_announcement_catalog(
        session,
        "300502",
        start,
        end,
        evaluated_at=acquired_at + timedelta(hours=23, minutes=59),
    )
    assert selected.executable is True


def test_announcement_stales_only_after_exact_24_hours(session):
    acquired_at = datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    start, end, _, _ = _persist_catalog_at(session, acquired_at)
    at_boundary = resolve_cached_announcement_catalog(
        session,
        "300502",
        start,
        end,
        evaluated_at=acquired_at + timedelta(hours=24),
    )
    after_boundary = resolve_cached_announcement_catalog(
        session,
        "300502",
        start,
        end,
        evaluated_at=acquired_at + timedelta(hours=24, seconds=1),
    )
    assert at_boundary.executable is True
    assert after_boundary.effective_quality.effective_quality == DataQualityStatus.STALE
    assert after_boundary.executable is False


def test_market_naive_storage_is_interpreted_as_shanghai():
    stored = datetime(2026, 7, 24, 10, 0)
    assert market_storage_naive_to_aware(stored) == datetime(
        2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ
    )


def test_research_naive_storage_is_interpreted_as_utc():
    stored = datetime(2026, 7, 24, 2, 0)
    converted = utc_storage_naive_to_aware(stored)
    assert converted == datetime(2026, 7, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    assert stored.replace(tzinfo=timezone.utc).astimezone(SHANGHAI_TZ) == converted


def test_profile_conflict_does_not_overwrite_cache(session):
    router = _router(session)
    original = router.company_profile("300502")
    profile = persist_company_profile(session, router, original)
    original_name = profile.name

    second_registry = ProviderRegistry()
    first = ResearchProvider()
    second = ResearchProvider()
    second.metadata = ProviderMetadata(
        provider_id="research-conflict",
        supported_capabilities=("fundamental.profile",),
        priority=2,
    )
    second.company_profile = lambda symbol: {"A股简称": "不同公司", "细分行业": "测试行业"}
    second_registry.register(first)
    second_registry.register(second)
    conflict_router = DataHubRouter(session, second_registry)
    conflict = conflict_router.company_profile("300502")
    assert conflict.quality_status.value == "CONFLICTED"
    with pytest.raises(ProviderUnavailableError):
        persist_company_profile(session, conflict_router, conflict)
    assert profile.name == original_name
    assert profile.quality_record_id == original.quality_record_id


def test_profile_missing_attempt_preserves_fresh_cache(session):
    router = _router(session)
    result = router.company_profile("300502")
    persist_company_profile(session, router, result)
    missing_router = _scenario_router(
        session, ScenarioResearchProvider("missing", fail_profile=True)
    )
    assert missing_router.company_profile("300502").quality_status.value == "MISSING"
    selected = resolve_cached_company_profile(session, "300502")
    assert selected.executable is True
    assert selected.quality_record_id == result.quality_record_id


def test_new_profile_conflict_blocks_old_profile(session):
    router = _router(session)
    result = router.company_profile("300502")
    persist_company_profile(session, router, result)
    conflict_router = _scenario_router(
        session,
        ScenarioResearchProvider("profile-a", priority=1),
        ScenarioResearchProvider(
            "profile-b",
            profile={"A股简称": "不同公司", "细分行业": "测试行业"},
            priority=2,
        ),
    )
    conflict = conflict_router.company_profile("300502")
    selected = resolve_cached_company_profile(session, "300502")
    assert conflict.quality_status.value == "CONFLICTED"
    assert selected.effective_quality.effective_quality.value == "CONFLICTED"
    assert selected.executable is False


def test_empty_scan_digest_is_stable(session):
    start, end = date.today() - timedelta(days=30), date.today()
    first = _router(session).company_announcements("300502", start, end)
    second = _router(session).company_announcements("300502", start, end)
    assert first.normalized_digest == second.normalized_digest


def test_old_announcement_with_fresh_scan_is_valid(session):
    start, end = date.today() - timedelta(days=365), date.today()
    rows = [
        {
            "公告标题": "历史公告",
            "公告日期": start.isoformat(),
            "公告链接": "https://example.test/old-announcement",
        }
    ]
    router = _scenario_router(
        session, ScenarioResearchProvider("catalog", announcements=rows)
    )
    result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, router, result, start=start, end=end)
    selected = resolve_cached_announcement_catalog(session, "300502", start, end)
    assert selected.executable is True
    assert selected.announcements[0].published_date == start


def test_catalog_freshness_uses_checked_at_not_publication_date(session):
    start, end = date.today() - timedelta(days=365), date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    refresh = persist_announcement_catalog(
        session, router, result, start=start, end=end
    )
    selected = resolve_cached_announcement_catalog(
        session,
        "300502",
        start,
        end,
        evaluated_at=utc_storage_naive_to_aware(refresh.checked_at)
        + timedelta(hours=25),
    )
    assert selected.effective_quality.effective_quality.value == "STALE"
    assert selected.executable is False


def test_missing_scan_attempt_preserves_fresh_catalog(session):
    start, end = date.today() - timedelta(days=30), date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, router, result, start=start, end=end)
    missing_router = _scenario_router(
        session, ScenarioResearchProvider("missing", fail_announcements=True)
    )
    missing = missing_router.company_announcements("300502", start, end)
    selected = resolve_cached_announcement_catalog(session, "300502", start, end)
    assert missing.quality_status.value == "MISSING"
    assert selected.executable is True
    assert selected.quality_record_id == result.quality_record_id


def test_new_catalog_conflict_blocks_old_scan(session):
    start, end = date.today() - timedelta(days=30), date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, router, result, start=start, end=end)
    conflict_router = _scenario_router(
        session,
        ScenarioResearchProvider("catalog-a", announcements=[], priority=1),
        ScenarioResearchProvider(
            "catalog-b",
            announcements=[
                {
                    "公告标题": "新增公告",
                    "公告日期": end.isoformat(),
                    "公告链接": "https://example.test/new",
                }
            ],
            priority=2,
        ),
    )
    conflict = conflict_router.company_announcements("300502", start, end)
    selected = resolve_cached_announcement_catalog(session, "300502", start, end)
    assert conflict.quality_status.value == "CONFLICTED"
    assert selected.effective_quality.effective_quality.value == "CONFLICTED"
    assert selected.executable is False


def test_scan_coverage_mismatch_blocks(session):
    start, end = date.today() - timedelta(days=30), date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, router, result, start=start, end=end)
    selected = resolve_cached_announcement_catalog(
        session, "300502", start + timedelta(days=1), end
    )
    assert selected.executable is False


def test_legacy_refresh_without_lineage_is_missing(session):
    now = datetime.now()
    session.add(
        CompanyResearchRefresh(
            symbol="300502",
            section="announcements",
            status="success",
            row_count=0,
            cache_used=False,
            last_attempt_at=now,
            last_success_at=now,
            checked_at=now,
            scan_start=datetime.combine(date.today() - timedelta(days=30), datetime.min.time()),
            scan_end=datetime.combine(date.today(), datetime.max.time()),
        )
    )
    session.flush()
    selected = resolve_cached_announcement_catalog(
        session, "300502", date.today() - timedelta(days=30), date.today()
    )
    assert selected.executable is False
    assert selected.structure_reason == "legacy_missing_quality_lineage"


def test_unrelated_stock_catalog_conflict_does_not_block(session):
    start, end = date.today() - timedelta(days=30), date.today()
    router = _router(session)
    result = router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, router, result, start=start, end=end)
    conflict_router = _scenario_router(
        session,
        ScenarioResearchProvider("other-a", announcements=[], priority=1),
        ScenarioResearchProvider(
            "other-b",
            announcements=[
                {
                    "公告标题": "其他股票公告",
                    "公告日期": end.isoformat(),
                    "公告链接": "https://example.test/other",
                }
            ],
            priority=2,
        ),
    )
    assert conflict_router.company_announcements("000001", start, end).quality_status.value == "CONFLICTED"
    assert resolve_cached_announcement_catalog(
        session, "300502", start, end
    ).executable is True


def test_new_persisted_profile_requires_reanalysis(session):
    first_router = _router(session)
    first = first_router.company_profile("300502")
    persist_company_profile(session, first_router, first)
    second_router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "replacement",
            profile={"A股简称": "替换公司", "细分行业": "测试行业"},
        ),
    )
    second = second_router.company_profile("300502")
    persist_company_profile(session, second_router, second)
    selected = resolve_cached_company_profile(session, "300502")
    assert selected.quality_record_id == second.quality_record_id
    assert selected.quality_record_id != first.quality_record_id


def test_new_persisted_scan_requires_reanalysis(session):
    start, end = date.today() - timedelta(days=30), date.today()
    first_router = _router(session)
    first = first_router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, first_router, first, start=start, end=end)
    second_router = _scenario_router(
        session, ScenarioResearchProvider("replacement", announcements=[])
    )
    second = second_router.company_announcements("300502", start, end)
    persist_announcement_catalog(session, second_router, second, start=start, end=end)
    selected = resolve_cached_announcement_catalog(session, "300502", start, end)
    assert selected.quality_record_id == second.quality_record_id
    assert selected.quality_record_id != first.quality_record_id


def test_source_quality_binding_validates_announcement_coverage():
    checked = datetime(2026, 7, 25, 12, 0)
    binding = SourceQualityBinding(
        data_capability="announcement.catalog",
        subject_type="stock",
        subject_id="300502",
        semantic_key="catalog/2025-07-25/2026-07-25",
        quality_record_id=1,
        observed_at=checked,
        scan_start=datetime(2025, 7, 25),
        scan_end=datetime(2026, 7, 25, 23, 59, 59, 999999),
        checked_at=checked,
    )
    assert binding.checked_at == binding.observed_at
    with pytest.raises(ValueError):
        binding.model_copy(update={"semantic_key": "catalog/2025-07-26/2026-07-25"})
        SourceQualityBinding.model_validate(
            {
                **binding.model_dump(),
                "semantic_key": "catalog/2025-07-26/2026-07-25",
            }
        )


def test_source_binding_is_included_in_evidence_payload():
    binding = SourceQualityBinding(
        data_capability="fundamental.profile",
        subject_type="stock",
        subject_id="300502",
        semantic_key="profile",
        quality_record_id=1,
        observed_at=datetime(2026, 7, 25, 12, 0),
    )
    evidence = Evidence(
        evidence_id="pipeline:company_mapping",
        symbol="300502",
        capability="company_profile",
        required=True,
        category="company_mapping",
        source_name="research-test",
        observed_at="2026-07-25T12:00:00",
        quality_status=DataQualityStatus.SINGLE_SOURCE,
        source_quality_binding=binding,
        payload={},
    )
    assert evidence.model_dump(mode="json")["source_quality_binding"][
        "quality_record_id"
    ] == 1


def _assert_profile_payload_rejected(session, mutate):
    router = _router(session)
    result = router.company_profile("300502")
    mutate(result)
    with pytest.raises(
        ProviderUnavailableError,
        match="research persistence payload does not match quality lineage",
    ):
        persist_company_profile(session, router, result)
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert record.persisted is False
    assert session.query(CompanyProfile).count() == 0


def test_profile_payload_mutated_after_router_is_rejected(session):
    _assert_profile_payload_rejected(
        session, lambda result: result.value.update({"name": "tampered"})
    )


def test_profile_same_shape_content_tampering_is_rejected(session):
    def mutate(result):
        key = next(iter(result.value))
        result.value[key] = "same-shape-tampering"

    _assert_profile_payload_rejected(session, mutate)


def test_announcement_payload_mutated_after_router_is_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    rows = [_announcement_row(1, end)]
    router = _catalog_router(session, rows)
    result = router.company_announcements("300502", start, end)
    result.value.append(_announcement_row(2, end))
    with pytest.raises(ProviderUnavailableError, match="payload does not match"):
        persist_announcement_catalog(session, router, result, start=start, end=end)
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False
    assert session.query(CompanyAnnouncement).count() == 0


def test_announcement_same_length_content_tampering_is_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    router = _catalog_router(session, [_announcement_row(1, end)])
    result = router.company_announcements("300502", start, end)
    result.value[0]["公告标题"] = "same-length-tampering"
    with pytest.raises(ProviderUnavailableError, match="payload does not match"):
        persist_announcement_catalog(session, router, result, start=start, end=end)


@pytest.mark.parametrize("capability", ["profile", "announcement"])
def test_research_record_row_count_mismatch_is_rejected(session, capability):
    start, end = date.today() - timedelta(days=30), date.today()
    if capability == "profile":
        router = _router(session)
        result = router.company_profile("300502")

        def persist():
            return persist_company_profile(session, router, result)

    else:
        router = _catalog_router(session, [_announcement_row(1, end)])
        result = router.company_announcements("300502", start, end)

        def persist():
            return persist_announcement_catalog(
                session, router, result, start=start, end=end
            )
    session.get(DataQualityRecord, result.quality_record_id).row_count += 1
    session.flush()
    with pytest.raises(ProviderUnavailableError, match="payload does not match"):
        persist()


@pytest.mark.parametrize("target", ["result", "record"])
def test_recomputed_digest_must_match_result_and_record(session, target):
    router = _router(session)
    result = router.company_profile("300502")
    if target == "result":
        result.normalized_digest = "0" * 64
    else:
        session.get(DataQualityRecord, result.quality_record_id).normalized_digest = (
            "0" * 64
        )
        session.flush()
    with pytest.raises(ProviderUnavailableError, match="payload does not match"):
        persist_company_profile(session, router, result)


def test_exact_profile_payload_is_accepted(session):
    router = _router(session)
    result = router.company_profile("300502")
    profile = persist_company_profile(session, router, result)
    assert profile.raw_data == result.value
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert record.row_count == 1
    assert record.persisted is True


def test_exact_announcement_payload_is_accepted(session):
    start, end = date.today() - timedelta(days=30), date.today()
    rows = [_announcement_row(1, end)]
    _, result, refresh = _persist_catalog(session, rows, start, end)
    assert refresh.row_count == 1
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True


def test_profile_payload_mismatch_preserves_existing_profile(session):
    original_router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "profile-original", profile={"name": "original", "industry": "industry"}
        ),
    )
    original_result = original_router.company_profile("300502")
    original = persist_company_profile(session, original_router, original_result)
    session.commit()
    replacement_router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "profile-replacement",
            profile={"name": "replacement", "industry": "industry"},
        ),
    )
    replacement = replacement_router.company_profile("300502")
    replacement.value["name"] = "mutated"
    with pytest.raises(ProviderUnavailableError):
        persist_company_profile(session, replacement_router, replacement)
    session.commit()
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert stored.name == original.name == "original"
    assert stored.quality_record_id == original_result.quality_record_id


def test_profile_payload_mismatch_keeps_new_record_unpersisted(session):
    router = _router(session)
    result = router.company_profile("300502")
    result.value["name"] = "mutated"
    with pytest.raises(ProviderUnavailableError):
        persist_company_profile(session, router, result)
    session.commit()
    session.expire_all()
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False


def test_profile_business_write_failure_rolls_back_savepoint(session):
    original_router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "profile-original", profile={"name": "original", "industry": "industry"}
        ),
    )
    original_result = original_router.company_profile("300502")
    persist_company_profile(session, original_router, original_result)
    session.commit()
    replacement_router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "profile-replacement",
            profile={"name": "replacement", "industry": "industry"},
        ),
    )
    replacement = replacement_router.company_profile("300502")

    def fail_profile_write(current_session, flush_context, instances):
        if any(isinstance(item, CompanyProfile) for item in current_session.dirty):
            raise ValueError("simulated profile write failure")

    event.listen(session, "before_flush", fail_profile_write)
    try:
        with pytest.raises(ValueError, match="simulated profile write failure"):
            persist_company_profile(session, replacement_router, replacement)
    finally:
        event.remove(session, "before_flush", fail_profile_write)
    session.commit()
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert stored.name == "original"
    assert stored.quality_record_id == original_result.quality_record_id
    assert session.get(DataQualityRecord, replacement.quality_record_id).persisted is False


def _assert_invalid_catalog_rejected(session, rows, start, end):
    router = _catalog_router(session, rows, "invalid-catalog")
    result = router.company_announcements("300502", start, end)
    with pytest.raises(ProviderUnavailableError):
        persist_announcement_catalog(session, router, result, start=start, end=end)
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False


def test_malformed_announcement_row_is_rejected_before_delete(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _assert_invalid_catalog_rejected(session, ["not-a-mapping"], start, end)


def test_second_malformed_row_does_not_partially_replace_catalog(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _assert_invalid_catalog_rejected(
        session, [_announcement_row(1, end), "not-a-mapping"], start, end
    )
    assert session.query(CompanyAnnouncement).count() == 0


def test_announcement_outside_requested_coverage_is_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _assert_invalid_catalog_rejected(
        session, [_announcement_row(1, start - timedelta(days=1))], start, end
    )


def test_announcement_missing_url_is_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    row = _announcement_row(1, end)
    row["公告链接"] = ""
    _assert_invalid_catalog_rejected(session, [row], start, end)


def test_announcement_missing_title_is_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    row = _announcement_row(1, end)
    row["公告标题"] = ""
    _assert_invalid_catalog_rejected(session, [row], start, end)


def test_duplicate_announcement_urls_are_rejected(session):
    start, end = date.today() - timedelta(days=30), date.today()
    duplicate = "https://example.test/duplicate"
    rows = [
        _announcement_row(1, end, url=duplicate),
        _announcement_row(2, end, url=duplicate),
    ]
    _assert_invalid_catalog_rejected(session, rows, start, end)


def test_prepared_row_count_must_equal_provider_row_count(session):
    start, end = date.today() - timedelta(days=30), date.today()
    rows = [_announcement_row(1, end), _announcement_row(2, end)]
    rows[1]["公告链接"] = ""
    _assert_invalid_catalog_rejected(session, rows, start, end)
    assert session.query(CompanyAnnouncement).count() == 0


def _seed_complete_catalog(session, start, end, count=100):
    rows = [
        _announcement_row(index, start + timedelta(days=index % 30))
        for index in range(count)
    ]
    _, result, refresh = _persist_catalog(
        session, rows, start, end, provider_id="old-catalog"
    )
    session.commit()
    return rows, result, refresh


def _invalid_catalog_replacement_state(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _, old_result, _ = _seed_complete_catalog(session, start, end)
    replacement = [
        _announcement_row(index + 1000, start + timedelta(days=index % 30))
        for index in range(100)
    ]
    replacement[20] = "invalid-row-21"
    router = _catalog_router(session, replacement, "invalid-replacement")
    result = router.company_announcements("300502", start, end)
    with pytest.raises(ProviderUnavailableError):
        persist_announcement_catalog(session, router, result, start=start, end=end)
    session.commit()
    session.expire_all()
    refresh = session.query(CompanyResearchRefresh).filter_by(
        symbol="300502", section="announcements"
    ).one()
    new_record = session.get(DataQualityRecord, result.quality_record_id)
    selection = resolve_cached_announcement_catalog(session, "300502", start, end)
    return {
        "announcement_count": session.query(CompanyAnnouncement).count(),
        "old_quality_record_id": old_result.quality_record_id,
        "refresh_quality_record_id": refresh.quality_record_id,
        "new_record_persisted": new_record.persisted,
        "selection_executable": selection.executable,
    }


def test_invalid_catalog_preserves_all_existing_announcements(session):
    state = _invalid_catalog_replacement_state(session)
    assert state["announcement_count"] == 100
    assert state["selection_executable"] is True


def test_invalid_catalog_preserves_existing_refresh_lineage(session):
    state = _invalid_catalog_replacement_state(session)
    assert state["refresh_quality_record_id"] == state["old_quality_record_id"]


def test_invalid_catalog_keeps_new_quality_record_unpersisted(session):
    state = _invalid_catalog_replacement_state(session)
    assert state["new_record_persisted"] is False


def test_catalog_insert_failure_restores_old_catalog(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _, old_result, _ = _seed_complete_catalog(session, start, end, count=2)
    collision_url = "https://example.test/outside-collision"
    session.add(
        CompanyAnnouncement(
            symbol="300502",
            title="outside",
            announcement_category="其他公告",
            risk_level="灰",
            published_date=start - timedelta(days=1),
            catalog_source="test",
            exchange="深交所",
            url=collision_url,
            source_document_url=None,
            raw_data={},
            fetched_at=datetime.now(),
        )
    )
    session.commit()
    router = _catalog_router(
        session,
        [_announcement_row(999, end, url=collision_url)],
        "insert-failure",
    )
    result = router.company_announcements("300502", start, end)
    with pytest.raises(IntegrityError):
        persist_announcement_catalog(session, router, result, start=start, end=end)
    session.commit()
    session.expire_all()
    in_window = session.query(CompanyAnnouncement).filter(
        CompanyAnnouncement.published_date >= start,
        CompanyAnnouncement.published_date <= end,
    )
    assert in_window.count() == 2
    refresh = session.query(CompanyResearchRefresh).filter_by(
        symbol="300502", section="announcements"
    ).one()
    assert refresh.quality_record_id == old_result.quality_record_id
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is False


def test_complete_catalog_atomically_replaces_old_catalog(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _seed_complete_catalog(session, start, end, count=3)
    rows = [_announcement_row(101, end), _announcement_row(102, end)]
    _, result, refresh = _persist_catalog(
        session, rows, start, end, provider_id="new-catalog"
    )
    session.commit()
    assert session.query(CompanyAnnouncement).filter(
        CompanyAnnouncement.published_date >= start
    ).count() == 2
    assert refresh.row_count == 2
    assert refresh.quality_record_id == result.quality_record_id


def test_empty_catalog_atomically_replaces_old_catalog(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _seed_complete_catalog(session, start, end, count=3)
    _, result, refresh = _persist_catalog(
        session, [], start, end, provider_id="empty-catalog"
    )
    session.commit()
    assert session.query(CompanyAnnouncement).filter(
        CompanyAnnouncement.published_date >= start
    ).count() == 0
    assert refresh.row_count == 0
    assert session.get(DataQualityRecord, result.quality_record_id).persisted is True


def test_successful_empty_catalog_remains_executable(session):
    start, end = date.today() - timedelta(days=30), date.today()
    _persist_catalog(session, [], start, end, provider_id="empty-catalog")
    selected = resolve_cached_announcement_catalog(session, "300502", start, end)
    assert selected.announcements == []
    assert selected.executable is True


def _profile_state(profile):
    return {
        "name": profile.name,
        "industry": profile.industry,
        "source": profile.source,
        "raw_data": profile.raw_data,
        "fetched_at": profile.fetched_at,
        "quality_record_id": profile.quality_record_id,
    }


def _seed_profile_for_production_path(session):
    router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "old-profile",
            profile={"name": "old", "industry": "old-industry"},
        ),
    )
    result = router.company_profile("300502")
    profile = persist_company_profile(session, router, result)
    session.commit()
    return result, _profile_state(profile)


def test_sync_company_research_datahub_profile_uses_unified_persistence(
    session, monkeypatch
):
    router = _scenario_router(
        session,
        ScenarioResearchProvider(
            "production-profile",
            profile={"name": "canonical", "industry": "canonical-industry"},
        ),
    )
    original_persist = persist_company_profile

    def assert_clean_entry(db, current_router, result):
        assert not any(
            isinstance(item, CompanyProfile) and item.symbol == "300502"
            for item in db.new | db.dirty
        )
        return original_persist(db, current_router, result)

    monkeypatch.setattr(
        "app.services.company_research.persist_company_profile", assert_clean_entry
    )
    sync = sync_company_research(
        session, "300502", provider=router, include_documents=False
    )
    result = router.calls["fundamental.profile"]
    profile = session.query(CompanyProfile).filter_by(symbol="300502").one()
    record = session.get(DataQualityRecord, result.quality_record_id)
    assert sync["sections"]["profile"]["status"] == "success"
    assert profile.quality_record_id == record.id
    assert profile.raw_data == result.value
    assert record.persisted is True


def test_sync_company_research_tampered_profile_preserves_existing_cache(session):
    old_result, old_state = _seed_profile_for_production_path(session)

    def tamper(_router, result):
        result.value["name"] = "tampered"

    router = _mutating_profile_router(session, tamper)
    sync = sync_company_research(
        session, "300502", provider=router, include_documents=False
    )
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert sync["sections"]["profile"]["status"] == "cache_fallback"
    assert _profile_state(stored) == old_state
    assert stored.quality_record_id == old_result.quality_record_id
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False
    assert resolve_cached_company_profile(session, "300502").executable is True


def test_sync_company_research_rejected_new_profile_creates_no_business_row(session):
    def tamper(_router, result):
        result.value["name"] = "tampered"

    router = _mutating_profile_router(session, tamper)
    sync = sync_company_research(
        session, "300502", provider=router, include_documents=False
    )
    assert sync["sections"]["profile"]["status"] == "unavailable"
    assert session.query(CompanyProfile).filter_by(symbol="300502").count() == 0
    record = session.get(DataQualityRecord, router.profile_result.quality_record_id)
    assert record is not None
    assert record.persisted is False


def test_sync_company_research_row_count_mismatch_preserves_profile(session):
    _, old_state = _seed_profile_for_production_path(session)

    def alter_row_count(router, result):
        router.db.get(DataQualityRecord, result.quality_record_id).row_count += 1
        router.db.flush()

    router = _mutating_profile_router(session, alter_row_count)
    sync_company_research(session, "300502", provider=router, include_documents=False)
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert _profile_state(stored) == old_state
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False


def test_sync_company_research_digest_mismatch_preserves_profile(session):
    _, old_state = _seed_profile_for_production_path(session)

    def alter_digest(router, result):
        router.db.get(
            DataQualityRecord, result.quality_record_id
        ).normalized_digest = "0" * 64
        router.db.flush()

    router = _mutating_profile_router(session, alter_digest)
    sync_company_research(session, "300502", provider=router, include_documents=False)
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert _profile_state(stored) == old_state
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False


def test_sync_company_research_profile_mark_persisted_failure_rolls_back_business_write(
    session, monkeypatch
):
    _, old_state = _seed_profile_for_production_path(session)
    router = _mutating_profile_router(session, lambda _router, _result: None)
    original_mark_persisted = router.mark_persisted

    def fail_profile(result, cached_at=None):
        if result.capability == "fundamental.profile":
            raise ValueError("simulated profile mark_persisted failure")
        return original_mark_persisted(result, cached_at=cached_at)

    monkeypatch.setattr(router, "mark_persisted", fail_profile)
    sync = sync_company_research(
        session, "300502", provider=router, include_documents=False
    )
    session.expire_all()
    stored = session.query(CompanyProfile).filter_by(symbol="300502").one()
    assert sync["sections"]["profile"]["status"] == "cache_fallback"
    assert _profile_state(stored) == old_state
    assert session.get(
        DataQualityRecord, router.profile_result.quality_record_id
    ).persisted is False
