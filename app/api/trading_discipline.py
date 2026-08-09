from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.domain.hashing import canonical_hash
from app.errors import AppError
from app.models import (
    Account,
    PretradeDisciplineCheck,
    SourceEvidenceRecord,
    Trade,
    TradeDisciplineReview,
    TradeThesisSnapshot,
    TradingPlaybook,
    TradingTrainingProgram,
)
from app.trading_discipline.contracts import EvidenceTier, PreTradeContext, TradeDecisionInput
from app.trading_discipline.service import RULE_VERSION, TradingDisciplineService


router = APIRouter(prefix="/api/v1/trading-discipline", tags=["trading-discipline"])
service = TradingDisciplineService()


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaybookCreate(InputModel):
    code: str
    version: str
    name: str
    description: str
    active: bool = False
    rules: dict[str, Any] = Field(default_factory=dict)


class TrainingProgramCreate(InputModel):
    account_id: int
    playbook_id: int
    target_samples: int = Field(default=20, ge=1)
    status: str = "DRAFT"


class EvidenceCreate(InputModel):
    symbol: str | None = None
    scope: str | None = None
    title: str
    content_summary: str
    tier: EvidenceTier
    source_type: str
    source_reference: str | None = None
    published_at: datetime | None = None
    observed_at: datetime
    added_by: str
    before_or_after_entry: str
    verified: bool = False
    quality: str = "UNVERIFIED"
    lineage: dict[str, Any] = Field(default_factory=dict)
    related_thesis_id: str | None = None


class ThesisCreate(InputModel):
    thesis_id: str
    symbol: str
    account_id: int
    playbook_id: int
    entry_reasons: list[str]
    invalidation_conditions: list[str]
    expected_behavior: list[str]
    hard_stop: Decimal
    initial_position: int = Field(ge=0)
    max_position: int = Field(ge=0)
    fact_evidence_ids: list[int] = Field(default_factory=list)
    analysis_hypotheses: list[str] = Field(default_factory=list)
    strategy_run_refs: list[str] = Field(default_factory=list)
    parent_snapshot_id: int | None = None
    created_at: datetime


class ReviewCreate(InputModel):
    trade_id: int
    training_program_id: int | None = None
    pretrade_check_id: int | None = None
    thesis_snapshot_id: int | None = None
    stage_at_decision: str = "UNKNOWN"
    planned_position: int | None = None
    actual_position: int | None = None
    planned_entry: dict[str, Any] | None = None
    actual_entry: Decimal | None = None
    planned_stop: Decimal | None = None
    deviations: list[str] = Field(default_factory=list)
    post_entry_evidence_ids: list[int] = Field(default_factory=list)
    pnl_pct: Decimal | None = None
    mfe_pct: Decimal | None = None
    mae_pct: Decimal | None = None
    exit_reason: str | None = None
    error_codes: list[str] = Field(default_factory=list)


def row(item) -> dict:
    return {column.name: getattr(item, column.name) for column in item.__table__.columns}


@router.get("/playbooks")
def list_playbooks(db: Session = Depends(get_db)):
    return [row(item) for item in db.scalars(select(TradingPlaybook).order_by(TradingPlaybook.id))]


@router.post("/playbooks", status_code=status.HTTP_201_CREATED)
def create_playbook(payload: PlaybookCreate, db: Session = Depends(get_db)):
    item = TradingPlaybook(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return row(item)


@router.get("/training-programs")
def list_training_programs(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(TradingTrainingProgram).order_by(TradingTrainingProgram.id.desc())
    if account_id is not None:
        query = query.where(TradingTrainingProgram.account_id == account_id)
    return [row(item) for item in db.scalars(query)]


@router.post("/training-programs", status_code=status.HTTP_201_CREATED)
def create_training_program(payload: TrainingProgramCreate, db: Session = Depends(get_db)):
    if db.get(Account, payload.account_id) is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    if db.get(TradingPlaybook, payload.playbook_id) is None:
        raise AppError(404, "PLAYBOOK_NOT_FOUND", "训练模板不存在")
    item = TradingTrainingProgram(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return row(item)


@router.post("/pretrade-check")
def pretrade_check(payload: PreTradeContext, db: Session = Depends(get_db)):
    if db.get(Account, payload.account_id) is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    result = service.pretrade_check(payload)
    existing = db.scalar(
        select(PretradeDisciplineCheck).where(
            PretradeDisciplineCheck.snapshot_hash == result.snapshot_hash
        )
    )
    if existing is None:
        existing = PretradeDisciplineCheck(
            account_id=payload.account_id,
            symbol=payload.symbol,
            action=payload.action.value,
            decision_at=payload.decision_at,
            status=result.status.value,
            score=result.score,
            category_scores=result.category_scores,
            rules=[item.model_dump(mode="json") for item in result.rules],
            reason_codes=result.reason_codes,
            source_refs=payload.source_refs,
            snapshot_hash=result.snapshot_hash,
            rule_version=RULE_VERSION,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)
    return {
        "id": existing.id,
        **result.model_dump(mode="json"),
        "decision_meaning": "DISCIPLINE_LAYER_DID_NOT_BLOCK"
        if result.status.value == "PASS"
        else "DISCIPLINE_CONSTRAINT_ACTIVE",
    }


@router.post("/decision-card")
def decision_card(payload: TradeDecisionInput):
    """Seven ordered gates; never creates formal execution authority."""
    return service.seven_gate_decision(payload).model_dump(mode="json")


@router.get("/evidence")
def list_evidence(
    symbol: str | None = None, decision_at: datetime | None = None, db: Session = Depends(get_db)
):
    query = select(SourceEvidenceRecord).order_by(SourceEvidenceRecord.observed_at)
    if symbol is not None:
        query = query.where(SourceEvidenceRecord.symbol == symbol)
    if decision_at is not None:
        query = query.where(SourceEvidenceRecord.observed_at <= decision_at).where(
            (SourceEvidenceRecord.published_at.is_(None))
            | (SourceEvidenceRecord.published_at <= decision_at)
        )
    return [row(item) for item in db.scalars(query)]


@router.post("/evidence", status_code=status.HTTP_201_CREATED)
def create_evidence(payload: EvidenceCreate, db: Session = Depends(get_db)):
    item = SourceEvidenceRecord(**payload.model_dump(mode="python"))
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        **row(item),
        "formal_execution_authority": False,
        "may_modify_plan": payload.tier is EvidenceTier.FACT and payload.verified,
    }


@router.get("/theses")
def list_theses(thesis_id: str | None = None, db: Session = Depends(get_db)):
    query = select(TradeThesisSnapshot).order_by(
        TradeThesisSnapshot.thesis_id, TradeThesisSnapshot.revision
    )
    if thesis_id:
        query = query.where(TradeThesisSnapshot.thesis_id == thesis_id)
    return [row(item) for item in db.scalars(query)]


@router.post("/theses", status_code=status.HTTP_201_CREATED)
def create_thesis(payload: ThesisCreate, db: Session = Depends(get_db)):
    if payload.initial_position > payload.max_position:
        raise AppError(422, "INVALID_POSITION_PLAN", "初始仓位不能超过最大仓位")
    parent = (
        db.get(TradeThesisSnapshot, payload.parent_snapshot_id)
        if payload.parent_snapshot_id
        else None
    )
    if payload.parent_snapshot_id and parent is None:
        raise AppError(404, "THESIS_PARENT_NOT_FOUND", "原始论点快照不存在")
    revision = 1 if parent is None else parent.revision + 1
    digest_payload = payload.model_dump(mode="json", exclude={"parent_snapshot_id"}) | {
        "revision": revision,
        "parent_snapshot_hash": parent.snapshot_hash if parent else None,
    }
    digest = canonical_hash(digest_payload)
    existing = db.scalar(
        select(TradeThesisSnapshot).where(TradeThesisSnapshot.snapshot_hash == digest)
    )
    if existing:
        return row(existing)
    item = TradeThesisSnapshot(
        **payload.model_dump(mode="python"), revision=revision, snapshot_hash=digest
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return row(item)


@router.post("/reviews", status_code=status.HTTP_201_CREATED)
def create_review(payload: ReviewCreate, db: Session = Depends(get_db)):
    trade = db.get(Trade, payload.trade_id)
    if trade is None:
        raise AppError(404, "TRADE_NOT_FOUND", "交易不存在")
    check = (
        db.get(PretradeDisciplineCheck, payload.pretrade_check_id)
        if payload.pretrade_check_id
        else None
    )
    compliant = check is not None and check.score == 100 and not payload.error_codes
    profitable = payload.pnl_pct is not None and payload.pnl_pct > 0
    classification = ("PROFITABLE" if profitable else "LOSING") + (
        "_COMPLIANT" if compliant else "_UNDISCIPLINED"
    )
    item = TradeDisciplineReview(
        **payload.model_dump(mode="python"),
        compliance_result=classification,
        rule_version=RULE_VERSION,
    )
    db.add(item)
    if payload.training_program_id:
        program = db.get(TradingTrainingProgram, payload.training_program_id)
        if program:
            program.valid_samples += 1
            if program.valid_samples >= program.target_samples:
                program.status = "COMPLETED"
                program.completed_at = datetime.now()
    db.commit()
    db.refresh(item)
    return row(item)


@router.get("/trades/{trade_id}/review")
def get_review(trade_id: int, db: Session = Depends(get_db)):
    item = db.scalar(
        select(TradeDisciplineReview)
        .where(TradeDisciplineReview.trade_id == trade_id)
        .order_by(TradeDisciplineReview.id.desc())
    )
    if item is None:
        raise AppError(404, "DISCIPLINE_REVIEW_NOT_FOUND", "纪律复盘不存在")
    return row(item)


@router.get("/dashboard")
def dashboard(account_id: int = Query(...), db: Session = Depends(get_db)):
    program = db.scalar(
        select(TradingTrainingProgram)
        .where(TradingTrainingProgram.account_id == account_id)
        .order_by(TradingTrainingProgram.id.desc())
    )
    reviews = list(
        db.scalars(
            select(TradeDisciplineReview)
            .join(Trade, Trade.id == TradeDisciplineReview.trade_id)
            .where(Trade.account_id == account_id)
            .order_by(TradeDisciplineReview.id.desc())
        )
    )
    checks = list(
        db.scalars(
            select(PretradeDisciplineCheck).where(PretradeDisciplineCheck.account_id == account_id)
        )
    )
    total = len(reviews)
    errors: dict[str, int] = {}
    for review in reviews:
        for code in review.error_codes:
            errors[code] = errors.get(code, 0) + 1
    evidence_distribution = dict(
        db.execute(
            select(SourceEvidenceRecord.tier, func.count())
            .where(
                SourceEvidenceRecord.symbol.in_(
                    select(Trade.symbol).where(Trade.account_id == account_id)
                )
            )
            .group_by(SourceEvidenceRecord.tier)
        ).all()
    )
    return {
        "program": row(program) if program else None,
        "progress": {
            "valid": program.valid_samples if program else 0,
            "target": program.target_samples if program else 20,
        },
        "average_discipline_score": sum(item.score for item in checks) / len(checks)
        if checks
        else None,
        "planned_trade_ratio": sum(
            1 for item in reviews if "UNPLANNED_TRADE" not in item.error_codes
        )
        / total
        if total
        else None,
        "unplanned_trade_ratio": sum(1 for item in reviews if "UNPLANNED_TRADE" in item.error_codes)
        / total
        if total
        else None,
        "chase_risk_trade_ratio": sum(1 for item in reviews if "CHASE_ENTRY" in item.error_codes)
        / total
        if total
        else None,
        "stop_compliance_ratio": sum(
            1 for item in reviews if "STOP_OVERRIDE_ATTEMPT" not in item.error_codes
        )
        / total
        if total
        else None,
        "position_limit_breaches": sum(
            1 for item in reviews if "POSITION_ABOVE_PLAN" in item.error_codes
        ),
        "thesis_change_count": sum(
            1 for item in reviews if "PLAN_CHANGED_AFTER_ENTRY" in item.error_codes
        ),
        "post_position_new_reason_count": sum(
            1 for item in reviews if "POST_POSITION_NEW_REASON" in item.error_codes
        ),
        "losing_position_add_attempts": sum(
            1 for item in reviews if "LOSS_AVERAGING" in item.error_codes
        ),
        "information_tier_distribution": evidence_distribution,
        "recurring_mistakes": sorted(
            ({"code": code, "count": count} for code, count in errors.items()),
            key=lambda item: (-item["count"], item["code"]),
        ),
        "recent_trades": [row(item) for item in reviews[:10]],
        "playbook_quality_conclusion": None
        if not program or program.valid_samples < program.target_samples
        else "TRAINING_SAMPLE_COMPLETE_NOT_PROFITABILITY_PROOF",
    }
