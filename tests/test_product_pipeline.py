from __future__ import annotations

import os
import time
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.data_hub.contracts import MarketAmountDaily
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ, to_market_storage_naive
from app.domain.models import (
    DecisionPackage,
    Evidence,
    MarketQualityBinding,
    StrategyBinding,
)
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef
from app.models import DataQualityRecord, MarketDailyBar, MarketRegimeSnapshot
from app.services.product_pipeline import (
    ProductDataState,
    RequiredDataAction,
    build_required_data_plan,
    refresh_product_data,
    run_product_pipeline,
    should_run_product_pipeline,
    snapshot_from_product_data,
)
from app.services.product_data import persist_product_result
from app.services.plan_freeze import _validate_strategy_bindings
from app.composition.strategies import get_strategy_registry
from app.errors import AppError
from app.strategies.contracts import StrategySignal
from test_domain_and_data_hub import _decision_package
from test_product_data_foundation import FullProductProvider
from test_strategy_contracts import FixtureStrategy, Snapshot


NOW = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)


def _state(
    capability: str,
    *,
    executable: bool,
    quality: DataQualityStatus,
    cache_used: bool = False,
    required: bool = True,
) -> ProductDataState:
    return ProductDataState(
        capability=capability,
        subject=SubjectRef(
            subject_type="stock",
            subject_id="300502",
            semantic_key=f"{capability}/scope",
        ),
        required=required,
        quality_status=quality,
        executable=executable,
        cache_used=cache_used,
        fallback_used=False,
    )


def test_required_data_plan_uses_fresh_cache_without_refresh():
    plan = build_required_data_plan(
        symbol="300502",
        analysis_started_at=NOW,
        states=(
            _state(
                "market.intraday.60m",
                executable=True,
                quality=DataQualityStatus.SINGLE_SOURCE,
                cache_used=True,
            ),
        ),
        force_refresh=False,
    )
    assert plan.items[0].action == RequiredDataAction.USE_CACHE


def test_required_data_plan_refreshes_missing_required_data_automatically():
    plan = build_required_data_plan(
        symbol="300502",
        analysis_started_at=NOW,
        states=(
            _state(
                "market.intraday.60m",
                executable=False,
                quality=DataQualityStatus.MISSING,
            ),
        ),
        force_refresh=False,
    )
    assert plan.items[0].action == RequiredDataAction.REFRESH


def test_required_data_plan_blocks_missing_required_data_after_refresh_attempt():
    plan = build_required_data_plan(
        symbol="300502",
        analysis_started_at=NOW,
        states=(
            _state(
                "market.intraday.60m",
                executable=False,
                quality=DataQualityStatus.MISSING,
            ),
        ),
        force_refresh=False,
        refresh_attempted=True,
    )
    assert plan.items[0].action == RequiredDataAction.BLOCK


def test_force_refresh_bypasses_fresh_executable_cache():
    plan = build_required_data_plan(
        symbol="300502",
        analysis_started_at=NOW,
        states=(
            _state(
                "market.intraday.60m",
                executable=True,
                quality=DataQualityStatus.SINGLE_SOURCE,
                cache_used=True,
            ),
        ),
        force_refresh=True,
    )
    assert plan.items[0].action == RequiredDataAction.REFRESH


def test_optional_data_becomes_optional_missing_after_refresh_attempt():
    plan = build_required_data_plan(
        symbol="300502",
        analysis_started_at=NOW,
        states=(
            _state(
                "company.concepts",
                executable=False,
                quality=DataQualityStatus.MISSING,
                required=False,
            ),
        ),
        force_refresh=False,
        refresh_attempted=True,
    )
    assert plan.items[0].action == RequiredDataAction.OPTIONAL


def test_required_data_plan_rejects_duplicate_capability_scope():
    state = _state(
        "market.intraday.60m",
        executable=True,
        quality=DataQualityStatus.SINGLE_SOURCE,
    )
    with pytest.raises(ValidationError, match="duplicate required data scope"):
        build_required_data_plan(
            symbol="300502",
            analysis_started_at=NOW,
            states=(state, state),
            force_refresh=False,
        )


def test_unresolved_industry_remains_an_explicit_required_block(session):
    registry = ProviderRegistry()
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    plan, _ = refresh_product_data(
        session,
        router,
        symbol="300502",
        industry=None,
        analysis_started_at=NOW,
        force_refresh=False,
    )
    industry_items = {
        item.capability: item
        for item in plan.items
        if item.capability.startswith("market.industry")
    }
    assert set(industry_items) == {
        "market.industry.daily",
        "market.industry.constituents",
    }
    assert all(item.action == RequiredDataAction.BLOCK for item in industry_items.values())
    assert all(item.quality_status == DataQualityStatus.MISSING for item in industry_items.values())


def test_first_symbol_without_product_cache_enters_product_pipeline(session):
    registry = ProviderRegistry()
    registry.register(FullProductProvider())
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    assert should_run_product_pipeline(session, router, "300502") is True


def test_index_binding_loads_real_persisted_history_into_product_snapshot(session):
    observed_at = datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)
    record = DataQualityRecord(
        symbol="CSI000300",
        capability="market.index_daily",
        subject_type="index",
        subject_id="CSI000300",
        semantic_key="unadjusted/CNY/share",
        quality_status="SINGLE_SOURCE",
        observed_at=to_market_storage_naive(observed_at),
        fetched_at=to_market_storage_naive(NOW),
        provider_id="fixture",
        provider_observations=[],
        normalized_digest="a" * 64,
        conflict_fields=[],
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        row_count=2,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    for trade_date, close in (
        (date(2026, 7, 23), Decimal("100")),
        (date(2026, 7, 24), Decimal("101")),
    ):
        session.add(
            MarketDailyBar(
                symbol="CSI000300",
                trade_date=trade_date,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000"),
                adjustment="unadjusted",
                price_unit="CNY",
                volume_unit="share",
                observed_at=to_market_storage_naive(observed_at),
                quality_status="SINGLE_SOURCE",
                quality_record_id=record.id,
                source="fixture",
                fetched_at=to_market_storage_naive(NOW),
            )
        )
    session.commit()
    evidence = Evidence(
        evidence_id="pipeline:market_judgement",
        symbol="300502",
        capability="market.index_daily",
        required=True,
        category="market_judgement",
        source_name="fixture",
        observed_at=observed_at.isoformat(),
        fetched_at=NOW.isoformat(),
        quality_status=DataQualityStatus.SINGLE_SOURCE,
        market_quality_binding=MarketQualityBinding(
            data_capability="market.index_daily",
            subject_type="index",
            subject_id="CSI000300",
            semantic_key="unadjusted/CNY/share",
            quality_record_id=record.id,
            observed_at=observed_at,
        ),
        payload={"summary": "index"},
        external_text_is_untrusted=False,
    )
    snapshot = snapshot_from_product_data(
        session,
        symbol="300502",
        analysis_started_at=NOW,
        selected={},
        legacy_evidence=(evidence,),
    )
    assert snapshot.capabilities[0].subject.subject_id == "CSI000300"
    assert [row["close"] for row in snapshot.capabilities[0].rows] == ["100.0000", "101.0000"]


def test_strategy_binding_is_deterministic_and_rejects_tampering():
    binding = StrategyBinding(
        strategy_id="core.discipline",
        strategy_version="1.0.0",
        implementation_hash="a" * 64,
        parameter_hash="b" * 64,
        signal_hash="c" * 64,
    )
    assert binding.binding_hash
    with pytest.raises(ValidationError, match="binding_hash"):
        StrategyBinding(
            **binding.model_dump(exclude={"binding_hash"}),
            binding_hash="d" * 64,
        )


def test_required_data_plan_hash_is_host_timezone_independent():
    state = _state(
        "market.intraday.60m",
        executable=True,
        quality=DataQualityStatus.SINGLE_SOURCE,
    )
    original = os.environ.get("TZ")
    hashes = []
    try:
        for zone in ("UTC", "Asia/Shanghai"):
            os.environ["TZ"] = zone
            if hasattr(time, "tzset"):
                time.tzset()
            hashes.append(
                build_required_data_plan(
                    symbol="300502",
                    analysis_started_at=NOW,
                    states=(state,),
                    force_refresh=False,
                ).plan_hash
            )
    finally:
        if original is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original
        if hasattr(time, "tzset"):
            time.tzset()
    assert hashes[0] == hashes[1]


def _preview():
    return {
        "status": "READY",
        "buy_plan": {
            "buy_zone": ["10.4209", "10.5391"],
            "hard_stop": "9.7023",
            "trigger_condition": "fixture trigger",
            "logic_invalidation": "fixture invalidation",
        },
        "position_calculation": {
            "final_allowed_quantity": 600,
            "trial_quantity": 100,
            "per_share_risk": "0.7777",
            "maximum_loss": "77.77",
        },
        "account": {},
    }


def _seed_product_data(session, *, include_breadth=True):
    provider = FullProductProvider()

    def amount_history(start, end):
        del start
        return [
            MarketAmountDaily(
                trade_date=end,
                total_amount="1200000000000",
                observed_at=datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ),
                source="fixture",
                fetched_at=NOW,
            )
        ]

    provider.get_market_amount_history = amount_history
    if not include_breadth:
        def missing_breadth(day):
            del day
            raise RuntimeError("market breadth unavailable")

        provider.get_market_breadth = missing_breadth
    registry = ProviderRegistry()
    registry.register(provider)
    router = DataHubRouter(session, registry, now_fn=lambda: NOW)
    calls = [
        router.get_intraday_60m(
            "300502",
            datetime(2026, 7, 24, 9, 30, tzinfo=SHANGHAI_TZ),
            NOW,
        ),
        router.get_turnover_daily("300502", NOW.date(), NOW.date()),
        router.get_market_amount_history(NOW.date(), NOW.date()),
        router.get_industry_universe(NOW.date(), NOW.date()),
        router.get_industry_constituents_universe(),
        router.company_concepts("300502"),
        router.company_industry_chain("300502"),
    ]
    if include_breadth:
        calls.append(router.get_market_breadth(NOW.date()))
    for result in calls:
        persist_product_result(session, router, result)
    session.commit()
    return router


def test_missing_breadth_blocks_market_snapshot_without_downgrading_other_data(
    session,
):
    router = _seed_product_data(session, include_breadth=False)
    observed_at = datetime(2026, 7, 24, 15, 0, tzinfo=SHANGHAI_TZ)
    record = DataQualityRecord(
        symbol="CSI000300",
        capability="market.index_daily",
        subject_type="index",
        subject_id="CSI000300",
        semantic_key="unadjusted/CNY/share",
        quality_status="SINGLE_SOURCE",
        observed_at=to_market_storage_naive(observed_at),
        fetched_at=to_market_storage_naive(NOW),
        provider_id="fixture",
        provider_observations=[],
        normalized_digest="b" * 64,
        conflict_fields=[],
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        row_count=2,
        trusted=True,
        persisted=True,
    )
    session.add(record)
    session.flush()
    for trade_date, close in (
        (date(2026, 7, 23), Decimal("100")),
        (date(2026, 7, 24), Decimal("101")),
    ):
        session.add(
            MarketDailyBar(
                symbol="CSI000300",
                trade_date=trade_date,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("1000"),
                adjustment="unadjusted",
                price_unit="CNY",
                volume_unit="share",
                observed_at=to_market_storage_naive(observed_at),
                quality_status="SINGLE_SOURCE",
                quality_record_id=record.id,
                source="fixture",
                fetched_at=to_market_storage_naive(NOW),
            )
        )
    session.commit()
    index_evidence = Evidence(
        evidence_id="pipeline:market_judgement",
        symbol="300502",
        capability="market.index_daily",
        required=True,
        category="market_judgement",
        source_name="fixture",
        observed_at=observed_at.isoformat(),
        fetched_at=NOW.isoformat(),
        quality_status=DataQualityStatus.SINGLE_SOURCE,
        market_quality_binding=MarketQualityBinding(
            data_capability="market.index_daily",
            subject_type="index",
            subject_id="CSI000300",
            semantic_key="unadjusted/CNY/share",
            quality_record_id=record.id,
            observed_at=observed_at,
        ),
        payload={"summary": "index"},
        external_text_is_untrusted=False,
    )

    result = run_product_pipeline(
        session,
        router,
        symbol="300502",
        industry="electronics",
        analysis_started_at=NOW,
        force_refresh=False,
        preview=_preview(),
        legacy_evidence=(index_evidence,),
    )

    by_capability = {item.capability: item for item in result.snapshot.capabilities}
    assert by_capability["market.index_daily"].executable is True
    assert by_capability["market.amount.daily"].executable is True
    assert by_capability["market.breadth.daily"].quality_status == DataQualityStatus.MISSING
    assert by_capability["market.breadth.daily"].rows == ()
    assert session.query(MarketRegimeSnapshot).count() == 0


def test_product_pipeline_builds_one_exact_lineage_snapshot(session):
    router = _seed_product_data(session)
    result = run_product_pipeline(
        session,
        router,
        symbol="300502",
        industry="electronics",
        analysis_started_at=NOW,
        force_refresh=False,
        preview=_preview(),
        legacy_evidence=(),
    )
    assert len(result.snapshot.capabilities) == 8
    by_capability = {item.capability: item for item in result.evidence}
    assert set(by_capability) == {
        "market.intraday.60m",
        "market.turnover.daily",
        "market.breadth.daily",
        "market.amount.daily",
        "market.industry.daily",
        "market.industry.constituents",
        "company.concepts",
        "company.industry_chain",
    }
    for evidence in result.evidence:
        if evidence.required:
            assert evidence.market_quality_binding is not None
            assert evidence.payload["normalized_digest"]
    chain_rows = next(
        item.rows
        for item in result.snapshot.capabilities
        if item.capability == "company.industry_chain"
    )
    assert chain_rows[0]["chain_name"] == "AI infrastructure"
    assert chain_rows[0]["node_name"] == "core equipment"
    assert result.concept_chain_context.chain_name == "AI infrastructure"
    assert result.concept_chain_context.node_name == "core equipment"
    assert result.concept_chain_context.stage == "CORE_EQUIPMENT"
    repeated = run_product_pipeline(
        session,
        router,
        symbol="300502",
        industry="electronics",
        analysis_started_at=NOW,
        force_refresh=False,
        preview=_preview(),
        legacy_evidence=(),
    )
    assert result.snapshot.snapshot_hash == repeated.snapshot.snapshot_hash


@pytest.mark.parametrize("previous_state", ["PANIC", "CONTRACTION"])
def test_product_pipeline_uses_persisted_previous_market_state_for_repair(
    session, previous_state
):
    session.add(
        MarketRegimeSnapshot(
            market_id="CN-A",
            trade_date=date(2026, 7, 23),
            state=previous_state,
            previous_state="DIVERGENCE",
            transition=f"DIVERGENCE->{previous_state}",
            product_snapshot_hash="a" * 64,
            observed_at=datetime(2026, 7, 23, 15, 0),
        )
    )
    session.commit()
    router = _seed_product_data(session)
    result = run_product_pipeline(
        session,
        router,
        symbol="300502",
        industry="electronics",
        analysis_started_at=NOW,
        force_refresh=False,
        preview=_preview(),
        legacy_evidence=(),
    )
    assert result.market_regime.state == "REPAIR"
    assert result.market_regime.transition == f"{previous_state}->REPAIR"


def _rehash(package: DecisionPackage) -> DecisionPackage:
    payload = package.model_dump(mode="json")
    payload["package_hash"] = package.package_hash_value()
    return DecisionPackage.model_validate(payload)


def test_strategy_binding_changes_decision_package_hash():
    base = _decision_package()
    first = _rehash(
        base.model_copy(
            update={
                "strategy_bindings": [
                    StrategyBinding(
                        strategy_id="core.discipline",
                        strategy_version="1.0.0",
                        implementation_hash="a" * 64,
                        parameter_hash="b" * 64,
                        signal_hash="c" * 64,
                    )
                ]
            }
        )
    )
    second = _rehash(
        base.model_copy(
            update={
                "strategy_bindings": [
                    StrategyBinding(
                        strategy_id="core.discipline",
                        strategy_version="1.0.0",
                        implementation_hash="a" * 64,
                        parameter_hash="d" * 64,
                        signal_hash="c" * 64,
                    )
                ]
            }
        )
    )
    assert first.package_hash != second.package_hash


def test_second_registered_strategy_executes_binds_and_is_blocked_when_disabled(session):
    registry = get_strategy_registry()
    strategy = FixtureStrategy()
    try:
        registry.register(strategy)
    except ValueError:
        registry.enable(strategy.strategy_id, strategy.strategy_version)
    router = _seed_product_data(session)
    result = run_product_pipeline(
        session,
        router,
        symbol="300502",
        industry="electronics",
        analysis_started_at=NOW,
        force_refresh=False,
        preview=_preview(),
        legacy_evidence=(),
    )
    assert "fixture.core" in {item.strategy_id for item in result.strategy_signals}
    signal = strategy.evaluate(Snapshot(), strategy.manifest().default_parameters)
    signal = StrategySignal.model_validate(
        {**signal.model_dump(exclude={"signal_hash"}), "evidence_refs": ()}
    )
    binding = StrategyBinding(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        implementation_hash=strategy.manifest().implementation_hash,
        parameter_hash=signal.parameter_hash,
        signal_hash=signal.signal_hash,
    )
    package = _rehash(
        _decision_package().model_copy(
            update={
                "strategy_bindings": [binding],
                "strategy_signals": [signal.model_dump(mode="json")],
            }
        )
    )
    _validate_strategy_bindings(package)
    registry.disable(strategy.strategy_id, strategy.strategy_version)
    with pytest.raises(AppError) as exc_info:
        _validate_strategy_bindings(package)
    assert exc_info.value.code == "STRATEGY_NOT_REGISTERED"
