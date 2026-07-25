from __future__ import annotations

import os
import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.data_hub.contracts import MarketAmountDaily
from app.data_hub.registry import ProviderRegistry
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.domain.models import DecisionPackage, StrategyBinding
from app.domain.quality import DataQualityStatus
from app.domain.quality_subject import SubjectRef
from app.services.product_pipeline import (
    ProductDataState,
    RequiredDataAction,
    build_required_data_plan,
    refresh_product_data,
    run_product_pipeline,
)
from app.services.product_data import persist_product_result
from test_domain_and_data_hub import _decision_package
from test_product_data_foundation import FullProductProvider


NOW = datetime(2026, 7, 24, 15, 30, tzinfo=SHANGHAI_TZ)


def _state(
    capability: str,
    *,
    executable: bool,
    quality: DataQualityStatus,
    cache_used: bool = False,
) -> ProductDataState:
    return ProductDataState(
        capability=capability,
        subject=SubjectRef(
            subject_type="stock",
            subject_id="300502",
            semantic_key=f"{capability}/scope",
        ),
        required=True,
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


def test_required_data_plan_refreshes_missing_required_data_when_authorized():
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
        force_refresh=True,
    )
    assert plan.items[0].action == RequiredDataAction.REFRESH


def test_required_data_plan_blocks_missing_required_data_without_refresh():
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
    assert plan.items[0].action == RequiredDataAction.BLOCK


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


def _seed_product_data(session):
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
        router.get_market_breadth(NOW.date()),
        router.get_market_amount_history(NOW.date(), NOW.date()),
        router.get_industry_daily("electronics", NOW.date(), NOW.date()),
        router.get_industry_constituents("electronics"),
        router.company_concepts("300502"),
        router.company_industry_chain("300502"),
    ]
    for result in calls:
        persist_product_result(session, router, result)
    session.commit()
    return router


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
