from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.domain.strategy_management import StrategyLifecycle
from app.domain.strategy_research import (
    EvidenceRecord,
    EvidenceType,
    StrategyResearchHistory,
    StrategyResearchRecord,
    ValidationRecord,
    ValidationStatus,
)
from app.domain.strategy_validation import ValidationReport, ValidationVerdict
from app.errors import AppError
from app.models import StrategyVersionRecord
from app.services.strategy_research.repository import SqlAlchemyStrategyResearchRepository
from app.services.transaction import transaction_scope


class StrategyResearchService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = SqlAlchemyStrategyResearchRepository(db)

    def _version(self, strategy_version_id: int) -> StrategyVersionRecord:
        version = self.db.get(StrategyVersionRecord, strategy_version_id)
        if version is None:
            raise AppError(404, "STRATEGY_VERSION_NOT_FOUND", "策略版本不存在")
        return version

    def create_research_record(
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
        with transaction_scope(self.db):
            version = self._version(strategy_version_id)
            if self.repository.research_record(strategy_version_id):
                raise AppError(409, "STRATEGY_RESEARCH_EXISTS", "该策略版本已有研究核心记录")
            if StrategyLifecycle(version.status) is StrategyLifecycle.ACTIVE:
                raise AppError(
                    409,
                    "ACTIVE_STRATEGY_RESEARCH_IMMUTABLE",
                    "ACTIVE策略版本的研究核心不可新增或改写",
                )
            result = self.repository.add_research(
                strategy_version_id=strategy_version_id,
                hypothesis=hypothesis,
                thesis=thesis,
                causal_chain=causal_chain,
                market_conditions=market_conditions,
                applicable_scenarios=applicable_scenarios,
                failure_conditions=failure_conditions,
            )
        return result

    def update_research_record(
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
        with transaction_scope(self.db):
            version = self._version(strategy_version_id)
            record = self.repository.research_record(strategy_version_id)
            if record is None:
                raise AppError(404, "STRATEGY_RESEARCH_NOT_FOUND", "研究核心记录不存在")
            if version.activated_at is not None:
                raise AppError(
                    409,
                    "ACTIVE_STRATEGY_RESEARCH_IMMUTABLE",
                    "已激活策略版本的研究核心不可修改，请创建新策略版本",
                )
            result = self.repository.update_research(
                record,
                hypothesis=hypothesis,
                thesis=thesis,
                causal_chain=causal_chain,
                market_conditions=market_conditions,
                applicable_scenarios=applicable_scenarios,
                failure_conditions=failure_conditions,
            )
        return result

    def add_evidence(
        self,
        *,
        strategy_version_id: int,
        evidence_type: EvidenceType,
        source: str,
        content: str,
        reference: str | None = None,
    ) -> EvidenceRecord:
        with transaction_scope(self.db):
            self._version(strategy_version_id)
            result = self.repository.add_evidence(
                strategy_version_id=strategy_version_id,
                evidence_type=evidence_type,
                source=source,
                content=content,
                reference=reference,
            )
        return result

    def add_validation(
        self,
        *,
        strategy_version_id: int,
        validation_type: str,
        status: ValidationStatus,
        sample_size: int,
        result_summary: str,
    ) -> ValidationRecord:
        if sample_size < 0:
            raise AppError(422, "VALIDATION_SAMPLE_SIZE_INVALID", "验证样本量不能为负数")
        with transaction_scope(self.db):
            self._version(strategy_version_id)
            result = self.repository.add_validation(
                strategy_version_id=strategy_version_id,
                validation_type=validation_type,
                status=status,
                sample_size=sample_size,
                result_summary=result_summary,
            )
        return result

    def record_validation_report(
        self,
        *,
        strategy_version_id: int,
        validation_type: str,
        report: ValidationReport,
    ) -> ValidationRecord:
        status = {
            ValidationVerdict.PASSED: ValidationStatus.PASSED,
            ValidationVerdict.FAILED: ValidationStatus.FAILED,
            ValidationVerdict.INSUFFICIENT_DATA: ValidationStatus.INSUFFICIENT_DATA,
        }[report.verdict]
        metrics = report.metrics
        summary = (
            f"verdict={report.verdict.value}; expectancy_r={metrics.expectancy_r}; "
            f"profit_factor={metrics.profit_factor}; max_drawdown_r="
            f"{metrics.maximum_drawdown_r}; tail_loss_r={metrics.tail_loss_r}; "
            f"cost_r={metrics.total_cost_r}; failures={list(report.failures)}; "
            f"warnings={list(report.warnings)}"
        )
        return self.add_validation(
            strategy_version_id=strategy_version_id,
            validation_type=validation_type,
            status=status,
            sample_size=metrics.sample_size,
            result_summary=summary,
        )

    def get_history(self, strategy_version_id: int) -> StrategyResearchHistory:
        self._version(strategy_version_id)
        return self.repository.get_history(strategy_version_id)

    def ensure_platform_breakout_research(
        self, strategy_version_id: int
    ) -> StrategyResearchHistory:
        """Idempotently register research for the pre-existing active strategy."""
        with transaction_scope(self.db):
            self._version(strategy_version_id)
            if self.repository.research_record(strategy_version_id) is None:
                self.repository.add_research(
                    strategy_version_id=strategy_version_id,
                    hypothesis="有效平台突破后缩量回踩、再次转强，能提供风险边界明确的趋势试仓机会。",
                    thesis="以平台结构确认供需平衡，以放量突破和缩量回踩过滤弱突破，并用再次转强确认执行。",
                    causal_chain=(
                        "平台整理形成可观察边界",
                        "放量突破显示需求增强",
                        "缩量回踩验证抛压减弱",
                        "再次转强触发试仓",
                        "结构失效或硬止损触发退出",
                    ),
                    market_conditions=("市场非明显下降", "周线非明显下降", "行情数据完整且新鲜"),
                    applicable_scenarios=("日线趋势波段", "平台突破后的首次有效回踩"),
                    failure_conditions=(
                        "数据不足或过期",
                        "未形成有效平台",
                        "突破后放量跌回平台",
                        "趋势或平台结构被破坏",
                        "止损距离或风险收益不合格",
                    ),
                )
                self.repository.add_evidence(
                    strategy_version_id=strategy_version_id,
                    evidence_type=EvidenceType.MANUAL,
                    source="existing_strategy_migration",
                    content="来自现有平台突破-回踩确认规则、Golden Master和纪律方法说明的结构化研究基线。",
                    reference="PlatformBreakoutPullbackStrategy 1.0.0",
                )
            result = self.repository.get_history(strategy_version_id)
        return result
