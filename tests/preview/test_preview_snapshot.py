from dataclasses import FrozenInstanceError, replace

import pytest

from app.services.preview_snapshot import create_snapshot, load_snapshot, verify_hash


def preview_payload() -> dict:
    return {
        "symbol": "300502",
        "preview_hash": "a" * 64,
        "status": "READY",
        "status_reason": "条件满足",
        "gates": [{"code": "market", "status": "通过"}],
        "pattern": {"breakout": True, "levels": [10.0, 10.5]},
        "multi_timeframe": {"weekly": {"state": "UP"}},
        "position_calculation": {"final_allowed_quantity": 400},
        "buy_plan": {"buy_zone": [10.2, 10.4], "hard_stop": 9.8},
        "exit_plan": {"target": 12.0},
        "rule": {"version": "1.2.0"},
        "account": {"equity": 100000},
        "sources": [{"id": "market-bars"}],
    }


def test_create_snapshot_freezes_complete_preview_and_hash():
    preview = preview_payload()
    snapshot = create_snapshot(preview)

    preview["pattern"]["breakout"] = False
    assert snapshot.feature_snapshot["pattern"]["breakout"] is True
    assert snapshot.preview_payload["buy_plan"]["buy_zone"] == (10.2, 10.4)
    assert verify_hash(snapshot)


def test_snapshot_is_recursively_immutable():
    snapshot = create_snapshot(preview_payload())

    with pytest.raises(FrozenInstanceError):
        snapshot.symbol = "000001"
    with pytest.raises(TypeError):
        snapshot.feature_snapshot["pattern"]["breakout"] = False


def test_hash_detects_changed_snapshot():
    snapshot = create_snapshot(preview_payload())
    assert not verify_hash(replace(snapshot, hash="0" * 64))


def test_load_snapshot_preserves_hash_and_frozen_data():
    original = create_snapshot(preview_payload(), snapshot_id=9)
    loaded = load_snapshot(
        {
            "snapshot_id": original.snapshot_id,
            "symbol": original.symbol,
            "preview_hash": original.preview_hash,
            "strategy_snapshot": original.strategy_snapshot,
            "feature_snapshot": original.feature_snapshot,
            "risk_snapshot": original.risk_snapshot,
            "decision_snapshot": original.decision_snapshot,
            "price_snapshot": original.price_snapshot,
            "rule_version_snapshot": original.rule_version_snapshot,
            "account_snapshot": original.account_snapshot,
            "market_snapshot": original.market_snapshot,
            "preview_payload": original.preview_payload,
            "created_at": original.created_at,
            "hash": original.hash,
        }
    )

    assert loaded.snapshot_id == 9
    assert verify_hash(loaded)
