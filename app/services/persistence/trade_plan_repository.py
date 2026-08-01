from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import RuleVersion, TradePlan, TradePlanAIAnalysis, TradePlanCheck


class TradePlanRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def latest(self, account_id: int, symbol: str) -> TradePlan | None:
        return self.db.scalar(
            select(TradePlan)
            .where(TradePlan.account_id == account_id, TradePlan.symbol == symbol)
            .order_by(TradePlan.plan_version.desc(), TradePlan.id.desc())
        )

    def save(self, plan: TradePlan) -> TradePlan:
        self.db.add(plan)
        self.db.flush()
        return plan

    def attach_ai_analysis(self, analysis_id: int | None, plan: TradePlan, preview: dict) -> None:
        if analysis_id is None:
            return
        analysis = self.db.get(TradePlanAIAnalysis, analysis_id)
        if (
            analysis is None
            or analysis.symbol != plan.symbol
            or analysis.evidence_package.get("preview_hash") != preview["preview_hash"]
        ):
            raise AppError(422, "AI_ANALYSIS_MISMATCH", "AI分析与当前股票或证据版本不匹配")
        analysis.trade_plan_id = plan.id

    def add_checks(self, plan: TradePlan, preview: dict, rule: RuleVersion) -> None:
        for gate in preview["gates"]:
            self.db.add(
                TradePlanCheck(
                    trade_plan_id=plan.id,
                    gate_code=gate["code"],
                    gate_name=gate["name"],
                    status=gate["status"],
                    basis=gate["evidence"],
                    missing_data=gate["missing_conditions"],
                    rule_version=rule.version,
                    checked_at=datetime.now(),  # noqa: DTZ005 - existing database convention
                )
            )

    def history(self, account_id: int, symbol: str) -> list[TradePlan]:
        return list(
            self.db.scalars(
                select(TradePlan)
                .where(TradePlan.account_id == account_id, TradePlan.symbol == symbol)
                .order_by(TradePlan.plan_version.desc(), TradePlan.id.desc())
            ).all()
        )

    def get(self, plan_id: int) -> TradePlan | None:
        return self.db.get(TradePlan, plan_id)

    def rule_version(self, rule_version_id: int) -> RuleVersion:
        version = self.db.get(RuleVersion, rule_version_id)
        if version is None:
            raise AppError(500, "RULE_VERSION_NOT_FOUND", "交易计划关联的规则版本不存在")
        return version
