from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.strategy_management import (
    StrategyDefinition,
    StrategyLifecycle,
    StrategyLifecycleEvent,
    StrategyVersion,
)
from app.models import (
    StrategyLifecycleEventRecord,
    StrategyRecord,
    StrategyVersionRecord,
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class SqlAlchemyStrategyRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _definition(record: StrategyRecord) -> StrategyDefinition:
        return StrategyDefinition(
            id=record.id,
            name=record.name,
            description=record.description,
            category=record.category,
            created_at=record.created_at,
            owner=record.owner,
            status=StrategyLifecycle(record.status),
        )

    def _version(self, record: StrategyVersionRecord) -> StrategyVersion:
        from app.services.strategy_research.repository import (
            SqlAlchemyStrategyResearchRepository,
        )

        history = SqlAlchemyStrategyResearchRepository(self.db).get_history(record.id)
        return StrategyVersion(
            id=record.id,
            strategy_id=record.strategy_id,
            version=record.version,
            status=StrategyLifecycle(record.status),
            rule_snapshot=record.rule_snapshot,
            parameter_snapshot=record.parameter_snapshot,
            created_at=record.created_at,
            activated_at=record.activated_at,
            retired_at=record.retired_at,
            research_record=history.research_record,
            evidence_records=history.evidence_records,
            validation_records=history.validation_records,
        )

    def strategy_record(self, strategy_id: str) -> StrategyRecord | None:
        return self.db.get(StrategyRecord, strategy_id)

    def version_record(self, strategy_id: str, version: str) -> StrategyVersionRecord | None:
        return self.db.scalar(
            select(StrategyVersionRecord).where(
                StrategyVersionRecord.strategy_id == strategy_id,
                StrategyVersionRecord.version == version,
            )
        )

    def get_strategy(self, strategy_id: str) -> StrategyDefinition | None:
        record = self.strategy_record(strategy_id)
        return self._definition(record) if record else None

    def get_version(self, strategy_id: str, version: str) -> StrategyVersion | None:
        record = self.version_record(strategy_id, version)
        return self._version(record) if record else None

    def get_version_by_id(self, version_id: int) -> StrategyVersion | None:
        record = self.db.get(StrategyVersionRecord, version_id)
        return self._version(record) if record else None

    def list_versions(self, strategy_id: str) -> list[StrategyVersion]:
        records = self.db.scalars(
            select(StrategyVersionRecord)
            .where(StrategyVersionRecord.strategy_id == strategy_id)
            .order_by(StrategyVersionRecord.id)
        ).all()
        return [self._version(record) for record in records]

    def add_strategy(
        self,
        *,
        strategy_id: str,
        name: str,
        description: str,
        category: str,
        owner: str,
        status: StrategyLifecycle = StrategyLifecycle.DRAFT,
    ) -> StrategyRecord:
        record = StrategyRecord(
            id=strategy_id,
            name=name,
            description=description,
            category=category,
            owner=owner,
            status=status.value,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def add_version(
        self,
        *,
        strategy_id: str,
        version: str,
        rule_snapshot: Mapping[str, Any],
        parameter_snapshot: Mapping[str, Any],
        status: StrategyLifecycle = StrategyLifecycle.DRAFT,
    ) -> StrategyVersionRecord:
        record = StrategyVersionRecord(
            strategy_id=strategy_id,
            version=version,
            status=status.value,
            rule_snapshot=_plain(rule_snapshot),
            parameter_snapshot=_plain(parameter_snapshot),
        )
        self.db.add(record)
        self.db.flush()
        return record

    def add_event(
        self,
        *,
        strategy_id: str,
        strategy_version_id: int | None,
        from_status: StrategyLifecycle | None,
        to_status: StrategyLifecycle,
        event_type: str,
        reason: str | None = None,
    ) -> StrategyLifecycleEventRecord:
        record = StrategyLifecycleEventRecord(
            strategy_id=strategy_id,
            strategy_version_id=strategy_version_id,
            from_status=from_status.value if from_status else None,
            to_status=to_status.value,
            event_type=event_type,
            reason=reason,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def list_events(self, strategy_id: str) -> list[StrategyLifecycleEvent]:
        records = self.db.scalars(
            select(StrategyLifecycleEventRecord)
            .where(StrategyLifecycleEventRecord.strategy_id == strategy_id)
            .order_by(StrategyLifecycleEventRecord.id)
        ).all()
        return [
            StrategyLifecycleEvent(
                id=record.id,
                strategy_id=record.strategy_id,
                strategy_version_id=record.strategy_version_id,
                from_status=StrategyLifecycle(record.from_status)
                if record.from_status
                else None,
                to_status=StrategyLifecycle(record.to_status),
                event_type=record.event_type,
                reason=record.reason,
                occurred_at=record.occurred_at,
            )
            for record in records
        ]
