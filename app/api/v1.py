from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import AppError
from app.models import Account, Holding
from app.schemas import (
    AccountCreate,
    AccountRead,
    AccountUpdate,
    HealthRead,
    HoldingCreate,
    HoldingRead,
    HoldingUpdate,
)
from app.services.portfolio import holding_metrics


router = APIRouter(prefix="/api/v1")


def get_account_or_404(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")
    return account


def get_holding_or_404(db: Session, holding_id: int) -> Holding:
    holding = db.get(Holding, holding_id)
    if holding is None:
        raise AppError(404, "HOLDING_NOT_FOUND", "持仓不存在")
    return holding


def serialize_holding(holding: Holding, account: Account) -> HoldingRead:
    values = {column.name: getattr(holding, column.name) for column in Holding.__table__.columns}
    values.update(holding_metrics(holding, account))
    return HoldingRead.model_validate(values)


@router.get("/health", response_model=HealthRead)
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "ok", "version": "0.1.0"}


@router.get("/accounts", response_model=list[AccountRead])
def list_accounts(db: Session = Depends(get_db)):
    return db.scalars(select(Account).order_by(Account.id)).all()


@router.post("/accounts", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    account = Account(**payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("/accounts/{account_id}", response_model=AccountRead)
def get_account(account_id: int, db: Session = Depends(get_db)):
    return get_account_or_404(db, account_id)


@router.put("/accounts/{account_id}", response_model=AccountRead)
def update_account(account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    for key, value in payload.model_dump().items():
        setattr(account, key, value)
    db.commit()
    db.refresh(account)
    return account


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(account_id: int, db: Session = Depends(get_db)):
    account = get_account_or_404(db, account_id)
    db.delete(account)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/holdings", response_model=list[HoldingRead])
def list_holdings(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(Holding).order_by(Holding.id)
    if account_id is not None:
        get_account_or_404(db, account_id)
        query = query.where(Holding.account_id == account_id)
    holdings = db.scalars(query).all()
    accounts = {item.id: item for item in db.scalars(select(Account)).all()}
    return [serialize_holding(item, accounts[item.account_id]) for item in holdings]


@router.post("/holdings", response_model=HoldingRead, status_code=status.HTTP_201_CREATED)
def create_holding(payload: HoldingCreate, db: Session = Depends(get_db)):
    account = get_account_or_404(db, payload.account_id)
    values = payload.model_dump()
    if values["price_source"] == "manual":
        values["price_updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    holding = Holding(**values)
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return serialize_holding(holding, account)


@router.get("/holdings/{holding_id}", response_model=HoldingRead)
def get_holding(holding_id: int, db: Session = Depends(get_db)):
    holding = get_holding_or_404(db, holding_id)
    return serialize_holding(holding, get_account_or_404(db, holding.account_id))


@router.put("/holdings/{holding_id}", response_model=HoldingRead)
def update_holding(holding_id: int, payload: HoldingUpdate, db: Session = Depends(get_db)):
    holding = get_holding_or_404(db, holding_id)
    account = get_account_or_404(db, payload.account_id)
    for key, value in payload.model_dump().items():
        setattr(holding, key, value)
    if payload.price_source == "manual":
        holding.price_updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(holding)
    return serialize_holding(holding, account)


@router.delete("/holdings/{holding_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_holding(holding_id: int, db: Session = Depends(get_db)):
    holding = get_holding_or_404(db, holding_id)
    db.delete(holding)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
