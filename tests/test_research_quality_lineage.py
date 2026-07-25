from datetime import date, datetime, timedelta

import pytest

from app.data_hub.contracts import DataProvider, ProviderMetadata, ProviderUnavailableError
from app.data_hub.registry import ProviderRegistry
from app.data_hub.research_subjects import (
    announcement_catalog_subject,
    company_profile_subject,
)
from app.data_hub.router import DataHubRouter
from app.models import CompanyProfile, CompanyResearchRefresh, DataQualityRecord
from app.domain.models import Evidence, SourceQualityBinding
from app.domain.quality import DataQualityStatus
from app.services.research_cache import (
    persist_announcement_catalog,
    persist_company_profile,
    resolve_cached_announcement_catalog,
    resolve_cached_company_profile,
)


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
        evaluated_at=refresh.checked_at + timedelta(hours=25),
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
