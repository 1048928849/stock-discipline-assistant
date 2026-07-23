from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.company_research import build_company_report, sync_company_research


router = APIRouter(prefix="/api/v1/company-research", tags=["公司研究中心"])


@router.post("/{symbol}/sync")
def sync_research(
    symbol: str = Path(pattern=r"^\d{6}$"),
    include_documents: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    return sync_company_research(db, symbol, include_documents=include_documents)


@router.get("/{symbol}")
def company_report(symbol: str = Path(pattern=r"^\d{6}$"), db: Session = Depends(get_db)):
    return build_company_report(db, symbol)
