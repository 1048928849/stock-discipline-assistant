from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.data_hub.trading_calendar import to_utc_storage_naive
from app.discovery.service import CandidateDiscoveryService, expire_candidate, promote_candidate
from app.errors import AppError
from app.models import (
    CandidateDiscoveryRun,
    CandidateIndustryAssessment,
    DiscoveryCandidate,
)


router = APIRouter(prefix="/api/discovery", tags=["candidate-discovery"])


class DiscoveryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidateStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CandidatePromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thesis: str = Field(min_length=1, max_length=4000)
    analysis_capital: Decimal = Field(default=Decimal("300000"), gt=0)
    waiting_conditions: list[str] = Field(default_factory=list, max_length=50)


def _run(db: Session, run_id: int) -> CandidateDiscoveryRun:
    row = db.get(CandidateDiscoveryRun, run_id)
    if row is None:
        raise AppError(404, "DISCOVERY_RUN_NOT_FOUND", "发现运行不存在")
    return row


def _candidate(db: Session, candidate_id: int) -> DiscoveryCandidate:
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise AppError(404, "DISCOVERY_CANDIDATE_NOT_FOUND", "候选不存在")
    if expire_candidate(row, now=datetime.now(timezone.utc)):
        db.commit()
        db.refresh(row)
    return row


@router.post("/runs", status_code=201)
def create_run(payload: DiscoveryRunRequest, db: Session = Depends(get_db)):
    return CandidateDiscoveryService(db).run()


@router.get("/runs")
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return db.scalars(
        select(CandidateDiscoveryRun)
        .order_by(CandidateDiscoveryRun.created_at.desc())
        .limit(limit)
    ).all()


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db)):
    return _run(db, run_id)


@router.get("/runs/{run_id}/industries")
def list_industries(run_id: int, db: Session = Depends(get_db)):
    _run(db, run_id)
    return db.scalars(
        select(CandidateIndustryAssessment)
        .where(CandidateIndustryAssessment.discovery_run_id == run_id)
        .order_by(CandidateIndustryAssessment.rank, CandidateIndustryAssessment.industry_key)
    ).all()


@router.get("/runs/{run_id}/candidates")
def list_candidates(run_id: int, db: Session = Depends(get_db)):
    _run(db, run_id)
    rows = db.scalars(
        select(DiscoveryCandidate)
        .where(DiscoveryCandidate.discovery_run_id == run_id)
        .order_by(DiscoveryCandidate.rank, DiscoveryCandidate.symbol)
    ).all()
    now = datetime.now(timezone.utc)
    changed = any(expire_candidate(row, now=now) for row in rows)
    if changed:
        db.commit()
    return rows


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: int, db: Session = Depends(get_db)):
    return _candidate(db, candidate_id)


@router.post("/candidates/{candidate_id}/review")
def review_candidate(
    candidate_id: int,
    _payload: CandidateStatusRequest,
    db: Session = Depends(get_db),
):
    row = _candidate(db, candidate_id)
    if row.status == "NEW":
        row.status = "REVIEWED"
        row.updated_at = to_utc_storage_naive(datetime.now(timezone.utc))
        db.commit()
        db.refresh(row)
    elif row.status != "REVIEWED":
        raise AppError(409, "DISCOVERY_CANDIDATE_INACTIVE", "候选不可审阅")
    return row


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: int,
    _payload: CandidateStatusRequest,
    db: Session = Depends(get_db),
):
    row = _candidate(db, candidate_id)
    if row.status in {"NEW", "REVIEWED"}:
        row.status = "REJECTED"
        row.updated_at = to_utc_storage_naive(datetime.now(timezone.utc))
        db.commit()
        db.refresh(row)
    elif row.status != "REJECTED":
        raise AppError(409, "DISCOVERY_CANDIDATE_INACTIVE", "候选不可拒绝")
    return row


@router.post("/candidates/{candidate_id}/promote")
def promote(
    candidate_id: int,
    payload: CandidatePromotionRequest,
    db: Session = Depends(get_db),
):
    return promote_candidate(
        db,
        candidate_id,
        thesis=payload.thesis,
        analysis_capital=payload.analysis_capital,
        waiting_conditions=payload.waiting_conditions,
    )


__all__ = ["router"]
