from collections.abc import Mapping
from typing import Any

from app.domain.trade_plan import TradePlanPreview, TradePlanSnapshot, TradePlanVersion


def confirm_preview(preview: TradePlanPreview, preview_hash: str) -> TradePlanSnapshot:
    return TradePlanSnapshot(
        symbol=preview.symbol,
        preview_hash=preview_hash,
        payload=preview.payload,
    )


def next_version(latest: Any | None) -> TradePlanVersion:
    version = (latest.plan_version or 1) + 1 if latest else 1
    return TradePlanVersion(version=version, parent_plan_id=latest.id if latest else None)


def lifecycle_from_execution_status(status: str | None) -> str:
    mapping = {
        "holding": "holding",
        "closed": "closed",
        "executing": "executing",
    }
    return mapping.get(status or "", "confirmed")


def snapshot_payload(snapshot: TradePlanSnapshot) -> Mapping[str, Any]:
    return snapshot.payload
