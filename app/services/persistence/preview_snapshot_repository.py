from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.preview.models import PreviewSnapshot, thaw
from app.models import PreviewSnapshotRecord
from app.services.preview_snapshot import create_snapshot, load_snapshot


class PreviewSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _payload(record: PreviewSnapshotRecord) -> dict:
        return {
            "snapshot_id": record.id,
            "symbol": record.symbol,
            "preview_hash": record.preview_hash,
            "strategy_snapshot": record.strategy_snapshot,
            "feature_snapshot": record.feature_snapshot,
            "risk_snapshot": record.risk_snapshot,
            "decision_snapshot": record.decision_snapshot,
            "price_snapshot": record.price_snapshot,
            "rule_version_snapshot": record.rule_version_snapshot,
            "account_snapshot": record.account_snapshot,
            "market_snapshot": record.market_snapshot,
            "preview_payload": record.preview_payload,
            "created_at": record.created_at,
            "hash": record.snapshot_hash,
        }

    def load(self, account_id: int, symbol: str, preview_hash: str) -> PreviewSnapshot | None:
        record = self.db.scalar(
            select(PreviewSnapshotRecord).where(
                PreviewSnapshotRecord.account_id == account_id,
                PreviewSnapshotRecord.symbol == symbol,
                PreviewSnapshotRecord.preview_hash == preview_hash,
            )
        )
        return load_snapshot(self._payload(record)) if record else None

    def save(self, account_id: int, preview: dict) -> PreviewSnapshot:
        existing = self.load(account_id, preview["symbol"], preview["preview_hash"])
        if existing is not None:
            return existing
        snapshot = create_snapshot(preview)
        record = PreviewSnapshotRecord(
            account_id=account_id,
            symbol=snapshot.symbol,
            preview_hash=snapshot.preview_hash,
            snapshot_hash=snapshot.hash,
            strategy_snapshot=thaw(snapshot.strategy_snapshot),
            feature_snapshot=thaw(snapshot.feature_snapshot),
            risk_snapshot=thaw(snapshot.risk_snapshot),
            decision_snapshot=thaw(snapshot.decision_snapshot),
            price_snapshot=thaw(snapshot.price_snapshot),
            rule_version_snapshot=thaw(snapshot.rule_version_snapshot),
            account_snapshot=thaw(snapshot.account_snapshot),
            market_snapshot=thaw(snapshot.market_snapshot),
            preview_payload=thaw(snapshot.preview_payload),
        )
        self.db.add(record)
        self.db.flush()
        return load_snapshot(self._payload(record))
