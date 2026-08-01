from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.domain.preview.models import PreviewSnapshot, thaw


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _snapshot_content(snapshot: PreviewSnapshot) -> dict:
    return {
        "symbol": snapshot.symbol,
        "preview_hash": snapshot.preview_hash,
        "strategy_snapshot": thaw(snapshot.strategy_snapshot),
        "feature_snapshot": thaw(snapshot.feature_snapshot),
        "risk_snapshot": thaw(snapshot.risk_snapshot),
        "decision_snapshot": thaw(snapshot.decision_snapshot),
        "price_snapshot": thaw(snapshot.price_snapshot),
        "rule_version_snapshot": thaw(snapshot.rule_version_snapshot),
        "account_snapshot": thaw(snapshot.account_snapshot),
        "market_snapshot": thaw(snapshot.market_snapshot),
        "preview_payload": thaw(snapshot.preview_payload),
    }


def _digest(content: dict) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def create_snapshot(
    preview: Mapping[str, Any],
    *,
    snapshot_id: int | None = None,
    created_at: datetime | None = None,
) -> PreviewSnapshot:
    payload = thaw(preview)
    fields = {
        "symbol": payload["symbol"],
        "preview_hash": payload["preview_hash"],
        "strategy_snapshot": {
            "gates": payload.get("gates", []),
            "status_reason": payload.get("status_reason"),
            "missing_conditions": payload.get("missing_conditions", []),
            "next_observations": payload.get("next_observations", []),
        },
        "feature_snapshot": {
            "pattern": payload.get("pattern", {}),
            "multi_timeframe": payload.get("multi_timeframe", {}),
            "chart": payload.get("chart", {}),
            "data_status": payload.get("data_status"),
            "data_date": payload.get("data_date"),
        },
        "risk_snapshot": {"position_calculation": payload.get("position_calculation", {})},
        "decision_snapshot": {
            "status": payload.get("status"),
            "current_buy_allowed": payload.get("current_buy_allowed"),
            "existing_position": payload.get("existing_position", {}),
        },
        "price_snapshot": {
            "buy_plan": payload.get("buy_plan", {}),
            "exit_plan": payload.get("exit_plan", {}),
            "confirmation_add": payload.get("confirmation_add", {}),
        },
        "rule_version_snapshot": payload.get("rule", {}),
        "account_snapshot": payload.get("account", {}),
        "market_snapshot": {
            "sources": payload.get("sources", []),
            "data_date": payload.get("data_date"),
        },
        "preview_payload": payload,
    }
    snapshot_hash = _digest(fields)
    return PreviewSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at or datetime.now(),  # noqa: DTZ005 - database stores local naive time
        hash=snapshot_hash,
        **fields,
    )


def load_snapshot(record: Mapping[str, Any]) -> PreviewSnapshot:
    return PreviewSnapshot(
        snapshot_id=record.get("snapshot_id"),
        symbol=record["symbol"],
        preview_hash=record["preview_hash"],
        strategy_snapshot=record["strategy_snapshot"],
        feature_snapshot=record["feature_snapshot"],
        risk_snapshot=record["risk_snapshot"],
        decision_snapshot=record["decision_snapshot"],
        price_snapshot=record["price_snapshot"],
        rule_version_snapshot=record["rule_version_snapshot"],
        account_snapshot=record["account_snapshot"],
        market_snapshot=record["market_snapshot"],
        preview_payload=record["preview_payload"],
        created_at=record["created_at"],
        hash=record["hash"],
    )


def verify_hash(snapshot: PreviewSnapshot) -> bool:
    return _digest(_snapshot_content(snapshot)) == snapshot.hash


def snapshot_payload(snapshot: PreviewSnapshot) -> dict:
    return thaw(snapshot.preview_payload)
