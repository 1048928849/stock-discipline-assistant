from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.strategy_research import (
    EvidenceRecord,
    EvidenceType,
    StrategyResearchHistory,
    StrategyResearchRecord,
    ValidationRecord,
    ValidationStatus,
)
from app.models import (
    StrategyEvidenceRecordModel,
    StrategyResearchRecordModel,
    StrategyValidationRecordModel,
)


class SqlAlchemyStrategyResearchRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @staticmethod
    def _research(record: StrategyResearchRecordModel) -> StrategyResearchRecord:
        return StrategyResearchRecord(
            id=record.id,
            strategy_version_id=record.strategy_version_id,
            hypothesis=record.hypothesis,
            thesis=record.thesis,
            causal_chain=tuple(record.causal_chain),
            market_conditions=tuple(record.market_conditions),
            applicable_scenarios=tuple(record.applicable_scenarios),
            failure_conditions=tuple(record.failure_conditions),
            created_at=record.created_at,
        )

    @staticmethod
    def _evidence(record: StrategyEvidenceRecordModel) -> EvidenceRecord:
        return EvidenceRecord(
            id=record.id,
            strategy_version_id=record.strategy_version_id,
            type=EvidenceType(record.type),
            source=record.source,
            content=record.content,
            reference=record.reference,
            created_at=record.created_at,
        )

    @staticmethod
    def _validation(record: StrategyValidationRecordModel) -> ValidationRecord:
        return ValidationRecord(
            id=record.id,
            strategy_version_id=record.strategy_version_id,
            validation_type=record.validation_type,
            status=ValidationStatus(record.status),
            sample_size=record.sample_size,
            result_summary=record.result_summary,
            created_at=record.created_at,
        )

    def research_record(self, strategy_version_id: int) -> StrategyResearchRecordModel | None:
        return self.db.scalar(
            select(StrategyResearchRecordModel).where(
                StrategyResearchRecordModel.strategy_version_id == strategy_version_id
            )
        )

    def add_research(
        self,
        *,
        strategy_version_id: int,
        hypothesis: str,
        thesis: str,
        causal_chain: Sequence[str],
        market_conditions: Sequence[str],
        applicable_scenarios: Sequence[str],
        failure_conditions: Sequence[str],
    ) -> StrategyResearchRecord:
        record = StrategyResearchRecordModel(
            strategy_version_id=strategy_version_id,
            hypothesis=hypothesis,
            thesis=thesis,
            causal_chain=list(causal_chain),
            market_conditions=list(market_conditions),
            applicable_scenarios=list(applicable_scenarios),
            failure_conditions=list(failure_conditions),
        )
        self.db.add(record)
        self.db.flush()
        return self._research(record)

    def update_research(
        self,
        record: StrategyResearchRecordModel,
        *,
        hypothesis: str,
        thesis: str,
        causal_chain: Sequence[str],
        market_conditions: Sequence[str],
        applicable_scenarios: Sequence[str],
        failure_conditions: Sequence[str],
    ) -> StrategyResearchRecord:
        record.hypothesis = hypothesis
        record.thesis = thesis
        record.causal_chain = list(causal_chain)
        record.market_conditions = list(market_conditions)
        record.applicable_scenarios = list(applicable_scenarios)
        record.failure_conditions = list(failure_conditions)
        self.db.flush()
        return self._research(record)

    def add_evidence(
        self,
        *,
        strategy_version_id: int,
        evidence_type: EvidenceType,
        source: str,
        content: str,
        reference: str | None,
    ) -> EvidenceRecord:
        record = StrategyEvidenceRecordModel(
            strategy_version_id=strategy_version_id,
            type=evidence_type.value,
            source=source,
            content=content,
            reference=reference,
        )
        self.db.add(record)
        self.db.flush()
        return self._evidence(record)

    def add_validation(
        self,
        *,
        strategy_version_id: int,
        validation_type: str,
        status: ValidationStatus,
        sample_size: int,
        result_summary: str,
    ) -> ValidationRecord:
        record = StrategyValidationRecordModel(
            strategy_version_id=strategy_version_id,
            validation_type=validation_type,
            status=status.value,
            sample_size=sample_size,
            result_summary=result_summary,
        )
        self.db.add(record)
        self.db.flush()
        return self._validation(record)

    def get_history(self, strategy_version_id: int) -> StrategyResearchHistory:
        research = self.research_record(strategy_version_id)
        evidence = self.db.scalars(
            select(StrategyEvidenceRecordModel)
            .where(StrategyEvidenceRecordModel.strategy_version_id == strategy_version_id)
            .order_by(StrategyEvidenceRecordModel.id)
        ).all()
        validations = self.db.scalars(
            select(StrategyValidationRecordModel)
            .where(StrategyValidationRecordModel.strategy_version_id == strategy_version_id)
            .order_by(StrategyValidationRecordModel.id)
        ).all()
        return StrategyResearchHistory(
            strategy_version_id=strategy_version_id,
            research_record=self._research(research) if research else None,
            evidence_records=tuple(self._evidence(record) for record in evidence),
            validation_records=tuple(self._validation(record) for record in validations),
        )
