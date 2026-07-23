import csv
import hashlib
import io
from datetime import date, datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.errors import AppError
from app.models import (
    AIAnalysis,
    Account,
    BacktestRun,
    DisciplineRule,
    MarketDailyBar,
    MarketQuote,
    MarketSourceLog,
    SystemJob,
    Trade,
    TradeReview,
    XPost,
    XWatchAccount,
    XWatchQuery,
)
from app.providers.llm_provider import LLMUnavailableError, OpenAICompatibleProvider
from app.providers.market import ProviderUnavailableError, Quote
from app.schemas_advanced import (
    BacktestCreate,
    DisciplineAlert,
    DisciplineRuleCreate,
    ReviewCreate,
    ReviewRead,
    TradeCreate,
    TradeRead,
    WatchAccountCreate,
    WatchQueryCreate,
)
from app.services.backtests import run_backtest
from app.services.discipline import check_discipline
from app.services.data_sources import UnifiedDataService
from app.services.reviews import review_metrics


router = APIRouter(prefix="/api/v1")


def account_exists(db: Session, account_id: int) -> None:
    if db.get(Account, account_id) is None:
        raise AppError(404, "ACCOUNT_NOT_FOUND", "账户不存在")


@router.get("/trades", response_model=list[TradeRead])
def list_trades(account_id: int | None = None, db: Session = Depends(get_db)):
    query = select(Trade).order_by(Trade.traded_at.desc())
    if account_id is not None:
        account_exists(db, account_id)
        query = query.where(Trade.account_id == account_id)
    return db.scalars(query).all()


@router.post("/trades", response_model=TradeRead, status_code=status.HTTP_201_CREATED)
def create_trade(payload: TradeCreate, db: Session = Depends(get_db)):
    account_exists(db, payload.account_id)
    trade = Trade(**payload.model_dump())
    db.add(trade)
    db.commit()
    db.refresh(trade)
    return trade


@router.delete("/trades/{trade_id}", status_code=204)
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    trade = db.get(Trade, trade_id)
    if not trade:
        raise AppError(404, "TRADE_NOT_FOUND", "交易记录不存在")
    db.delete(trade)
    db.commit()


@router.get("/discipline/rules")
def list_discipline_rules(db: Session = Depends(get_db)):
    return db.scalars(select(DisciplineRule).order_by(DisciplineRule.code)).all()


@router.put("/discipline/rules/{code}")
def configure_discipline_rule(
    code: str, payload: DisciplineRuleCreate, db: Session = Depends(get_db)
):
    if code != payload.code:
        raise AppError(422, "RULE_CODE_MISMATCH", "路径与请求体中的规则代码不一致")
    rule = db.scalar(select(DisciplineRule).where(DisciplineRule.code == code))
    if not rule:
        rule = DisciplineRule(**payload.model_dump())
        db.add(rule)
    else:
        for key, value in payload.model_dump().items():
            setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/trades/import-csv")
async def import_trades_csv(
    account_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)
):
    account_exists(db, account_id)
    raw = await file.read()
    if len(raw) > 5_000_000:
        raise AppError(413, "FILE_TOO_LARGE", "CSV 文件不能超过 5MB")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    except UnicodeDecodeError as exc:
        raise AppError(422, "CSV_ENCODING_ERROR", "CSV 必须使用 UTF-8 编码") from exc
    required = {"symbol", "side", "quantity", "price", "traded_at"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise AppError(422, "CSV_COLUMNS_ERROR", f"CSV 缺少必填列：{', '.join(sorted(required))}")
    inserted, duplicates, errors = 0, 0, []
    known_keys = set(db.scalars(select(Trade.import_key).where(Trade.import_key.is_not(None))))
    pending: list[Trade] = []
    for line_no, row in enumerate(reader, start=2):
        try:
            canonical = "|".join(str(row.get(k, "")).strip() for k in sorted(row))
            key = hashlib.sha256(f"{account_id}|{canonical}".encode()).hexdigest()
            if key in known_keys:
                duplicates += 1
                continue
            payload = TradeCreate(
                account_id=account_id,
                symbol=row["symbol"].strip(),
                side=row["side"].strip().upper(),
                quantity=int(row["quantity"]),
                price=row["price"],
                fee=row.get("fee") or 0,
                traded_at=datetime.fromisoformat(row["traded_at"]),
                reason=row.get("reason") or None,
                is_planned=str(row.get("is_planned", "true")).lower() in {"1", "true", "yes"},
                emotion=row.get("emotion") or None,
                expectation=row.get("expectation") or None,
                invalidation_condition=row.get("invalidation_condition") or None,
                notes=row.get("notes") or None,
            )
            pending.append(Trade(**payload.model_dump(), import_key=key))
            known_keys.add(key)
            inserted += 1
        except Exception as exc:
            errors.append({"line": line_no, "error": str(exc)[:300]})
    db.add_all(pending)
    db.commit()
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors}


@router.post("/reviews", response_model=ReviewRead, status_code=201)
def create_review(payload: ReviewCreate, db: Session = Depends(get_db)):
    review = TradeReview(**payload.model_dump())
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


@router.get("/reviews/daily")
def daily_review(
    account_id: int, day: date = Query(default_factory=date.today), db: Session = Depends(get_db)
):
    account_exists(db, account_id)
    return review_metrics(db, account_id, day, day)


@router.get("/reviews/weekly")
def weekly_review(account_id: int, week_start: date | None = None, db: Session = Depends(get_db)):
    account_exists(db, account_id)
    start = week_start or (date.today() - timedelta(days=date.today().weekday()))
    return review_metrics(db, account_id, start, start + timedelta(days=6))


@router.post("/discipline/check", response_model=list[DisciplineAlert])
def discipline_check(account_id: int, db: Session = Depends(get_db)):
    account_exists(db, account_id)
    return check_discipline(db, account_id)


@router.get("/market/quote/{symbol}")
def market_quote(symbol: str, db: Session = Depends(get_db)):
    quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
    if not quote:
        raise AppError(404, "QUOTE_NOT_FOUND", "尚无该股票行情，请先同步；不会返回伪造数据")
    return {
        "symbol": quote.symbol,
        "name": quote.name,
        "price": str(quote.price),
        "source": quote.source,
        "source_api": quote.source_api,
        "fetched_at": quote.fetched_at,
    }


def _live_market_call(callback):
    try:
        return callback()
    except ProviderUnavailableError as exc:
        raise AppError(503, "MARKET_DATA_UNAVAILABLE", str(exc)) from exc


@router.get("/market/symbols")
def market_symbols(db: Session = Depends(get_db)):
    return _live_market_call(lambda: UnifiedDataService(db).list_symbols().value)


@router.get("/market/indices")
def market_indices(
    family: str = Query(default="上证系列指数", max_length=30),
    db: Session = Depends(get_db),
):
    return _live_market_call(lambda: UnifiedDataService(db).list_indices(family).value)


@router.get("/market/sectors")
def market_sectors(db: Session = Depends(get_db)):
    return _live_market_call(lambda: UnifiedDataService(db).list_sectors().value)


@router.get("/market/announcements/{symbol}")
def market_announcements(
    symbol: str,
    day: date = Query(default_factory=date.today),
    db: Session = Depends(get_db),
):
    return _live_market_call(
        lambda: UnifiedDataService(db).daily_announcements(symbol, day).value
    )


@router.get("/market/history/{symbol}")
def market_history(
    symbol: str,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_db),
):
    query = (
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == symbol)
        .order_by(MarketDailyBar.trade_date)
    )
    if date_from:
        query = query.where(MarketDailyBar.trade_date >= date_from)
    if date_to:
        query = query.where(MarketDailyBar.trade_date <= date_to)
    return db.scalars(query).all()


@router.post("/market/sync")
def market_sync(
    symbol: str = Query(pattern=r"^\d{6}$"),
    days: int = Query(default=365, ge=30, le=3000),
    db: Session = Depends(get_db),
):
    running = db.scalar(
        select(SystemJob).where(
            SystemJob.name == f"market_sync:{symbol}", SystemJob.status == "running"
        )
    )
    if running:
        raise AppError(409, "JOB_ALREADY_RUNNING", "该股票同步任务正在运行，请勿重复提交")
    job = db.scalar(
        select(SystemJob).where(SystemJob.name == f"market_sync:{symbol}")
    ) or SystemJob(name=f"market_sync:{symbol}", status="pending")
    db.add(job)
    job.status = "running"
    job.started_at = datetime.now()
    db.commit()
    provider = UnifiedDataService(db)
    try:
        quote_error = None
        try:
            quote = provider.get_quote(symbol).value
        except ProviderUnavailableError as exc:
            quote = None
            quote_error = str(exc)
        history_result = provider.get_history(
            symbol, date.today() - timedelta(days=days), date.today()
        )
        bars = history_result.value
        if not bars:
            raise ProviderUnavailableError("历史行情为空，未写入数据库")
        dates = [bar.trade_date for bar in bars]
        if len(dates) != len(set(dates)) or any(
            bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
            for bar in bars
        ):
            raise ProviderUnavailableError("历史行情完整性检查失败，未写入数据库")
        if quote is None:
            latest = bars[-1]
            previous_quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
            quote = Quote(
                symbol=symbol,
                name=previous_quote.name if previous_quote and previous_quote.name else symbol,
                price=latest.close,
                source=latest.source,
                source_api="history_latest_close",
                fetched_at=latest.fetched_at,
            )
        stored = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol)) or MarketQuote(
            symbol=symbol,
            name=quote.name,
            price=quote.price,
            source=quote.source,
            source_api=quote.source_api,
            fetched_at=quote.fetched_at,
        )
        db.add(stored)
        stored.name = quote.name
        stored.price = quote.price
        stored.source = quote.source
        stored.source_api = quote.source_api
        stored.fetched_at = quote.fetched_at
        inserted = 0
        for bar in bars:
            existing = db.scalar(
                select(MarketDailyBar).where(
                    MarketDailyBar.symbol == symbol,
                    MarketDailyBar.trade_date == bar.trade_date,
                    MarketDailyBar.source == bar.source,
                )
            )
            if existing:
                existing.open = bar.open
                existing.high = bar.high
                existing.low = bar.low
                existing.close = bar.close
                existing.volume = bar.volume
                existing.fetched_at = bar.fetched_at
            else:
                db.add(MarketDailyBar(**bar.__dict__))
                inserted += 1
        history_sources = sorted({bar.source for bar in bars})
        db.add(
            MarketSourceLog(
                source=" + ".join([quote.source, *history_sources]),
                api_name=f"{quote.source_api} + {'/'.join(history_sources)}",
                status="success",
                error=f"实时行情降级原因：{quote_error}" if quote_error else None,
                row_count=inserted,
            )
        )
        job.status = "success"
        job.finished_at = datetime.now()
        job.result_count = inserted + 1
        job.last_error = None
        db.commit()
        return {
            "status": "success",
            "quote": {
                "symbol": symbol,
                "price": str(quote.price),
                "source": quote.source,
                "source_api": quote.source_api,
                "data_date": quote.fetched_at.date().isoformat(),
                "updated_at": quote.fetched_at.isoformat(),
                "status": "success",
            },
            "bars_inserted": inserted,
            "history_source": history_sources[0],
            "fallback_used": bool(quote_error) or history_result.fallback_used,
            "data_date": bars[-1].trade_date.isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
    except ProviderUnavailableError as exc:
        job.status = "failed"
        job.finished_at = datetime.now()
        job.last_error = str(exc)
        db.add(
            MarketSourceLog(
                source="akshare", api_name="market_sync", status="failed", error=str(exc)
            )
        )
        db.commit()
        cached_quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
        cached_bar = db.scalar(
            select(MarketDailyBar)
            .where(MarketDailyBar.symbol == symbol)
            .order_by(MarketDailyBar.trade_date.desc())
        )
        if cached_quote and cached_bar:
            return {
                "status": "stale_fallback",
                "message": "实时数据源暂不可用，已回退到最近一次成功数据；请勿视为当前成交价。",
                "error": str(exc),
                "quote": {
                    "symbol": symbol,
                    "price": str(cached_quote.price),
                    "source": cached_quote.source,
                    "source_api": cached_quote.source_api,
                    "data_date": cached_quote.fetched_at.date().isoformat(),
                    "updated_at": cached_quote.fetched_at.isoformat(),
                    "status": "stale",
                },
                "bars_inserted": 0,
                "history_source": cached_bar.source,
                "fallback_used": True,
                "data_date": cached_bar.trade_date.isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
        raise AppError(503, "MARKET_DATA_UNAVAILABLE", str(exc)) from exc


@router.get("/x/watch-accounts")
def watch_accounts(db: Session = Depends(get_db)):
    return db.scalars(select(XWatchAccount).order_by(XWatchAccount.username)).all()


@router.post("/x/watch-accounts", status_code=201)
def add_watch_account(payload: WatchAccountCreate, db: Session = Depends(get_db)):
    item = XWatchAccount(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/x/watch-accounts/{item_id}", status_code=204)
def delete_watch_account(item_id: int, db: Session = Depends(get_db)):
    item = db.get(XWatchAccount, item_id)
    if not item:
        raise AppError(404, "WATCH_ACCOUNT_NOT_FOUND", "关注账号不存在")
    db.delete(item)
    db.commit()


@router.get("/x/watch-queries")
def watch_queries(db: Session = Depends(get_db)):
    return db.scalars(select(XWatchQuery).order_by(XWatchQuery.name)).all()


@router.post("/x/watch-queries", status_code=201)
def add_watch_query(payload: WatchQueryCreate, db: Session = Depends(get_db)):
    item = XWatchQuery(**payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.delete("/x/watch-queries/{item_id}", status_code=204)
def delete_watch_query(item_id: int, db: Session = Depends(get_db)):
    item = db.get(XWatchQuery, item_id)
    if not item:
        raise AppError(404, "WATCH_QUERY_NOT_FOUND", "查询表达式不存在")
    db.delete(item)
    db.commit()


@router.get("/x/posts")
def x_posts(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)):
    return db.scalars(select(XPost).order_by(XPost.published_at.desc()).limit(limit)).all()


@router.post("/x/sync")
def x_sync(limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)):
    job = db.scalar(select(SystemJob).where(SystemJob.name == "x_sync"))
    if job and job.status == "running":
        raise AppError(409, "JOB_ALREADY_RUNNING", "X 同步任务正在运行，请勿重复提交")
    if not job:
        job = SystemJob(name="x_sync", status="pending")
        db.add(job)
    job.status = "running"
    job.started_at = datetime.now()
    job.last_error = None
    db.commit()
    expressions = [
        q.expression for q in db.scalars(select(XWatchQuery).where(XWatchQuery.enabled)).all()
    ]
    expressions += [
        f"from:{a.username}"
        for a in db.scalars(select(XWatchAccount).where(XWatchAccount.enabled)).all()
    ]
    if not expressions:
        job.status = "paused"
        job.finished_at = datetime.now()
        job.last_error = "尚未配置关注账号或查询表达式"
        db.commit()
        raise AppError(422, "X_QUERY_REQUIRED", "请先添加关注账号或查询表达式")
    try:
        posts = UnifiedDataService(db).collect_social_queries(expressions, limit).value
    except ProviderUnavailableError as exc:
        job.status = "paused"
        job.finished_at = datetime.now()
        job.last_error = str(exc)
        db.commit()
        raise AppError(503, "X_UNAVAILABLE", str(exc)) from exc
    inserted = 0
    for post in posts:
        if not db.scalar(
            select(XPost.id).where(XPost.platform == "x", XPost.post_id == post["post_id"])
        ):
            db.add(XPost(platform="x", **post))
            inserted += 1
    job.status = "success"
    job.finished_at = datetime.now()
    job.result_count = inserted
    db.commit()
    return {"status": "success", "collected": len(posts), "inserted": inserted}


@router.post("/ai/analyze/{content_id}")
def ai_analyze(content_id: int, db: Session = Depends(get_db)):
    post = db.get(XPost, content_id)
    if not post:
        raise AppError(404, "CONTENT_NOT_FOUND", "待分析的 X 内容不存在")
    provider = OpenAICompatibleProvider(get_settings())
    try:
        result = provider.analyze(post.content)
        analysis = AIAnalysis(
            content_type="x_post",
            content_id=post.id,
            source_url=post.url,
            source_time=post.published_at,
            model=get_settings().llm_model,
            status="success",
            result=result.model_dump(),
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
        return analysis
    except LLMUnavailableError as exc:
        analysis = AIAnalysis(
            content_type="x_post",
            content_id=post.id,
            source_url=post.url,
            source_time=post.published_at,
            model=get_settings().llm_model or "not-configured",
            status="failed",
            error=str(exc),
        )
        db.add(analysis)
        db.commit()
        raise AppError(503, "LLM_UNAVAILABLE", str(exc)) from exc


@router.post("/backtests", status_code=201)
def create_backtest(payload: BacktestCreate, db: Session = Depends(get_db)):
    bars = db.scalars(
        select(MarketDailyBar)
        .where(
            MarketDailyBar.symbol == payload.symbol,
            MarketDailyBar.trade_date >= payload.date_from,
            MarketDailyBar.trade_date <= payload.date_to,
        )
        .order_by(MarketDailyBar.trade_date)
    ).all()
    if not bars:
        raise AppError(422, "BACKTEST_DATA_REQUIRED", "没有可用历史数据，请先同步行情")
    grouped_bars: dict[str, list[MarketDailyBar]] = {}
    for bar in bars:
        grouped_bars.setdefault(bar.source, []).append(bar)
    data_source, bars = max(
        grouped_bars.items(), key=lambda pair: max(item.fetched_at for item in pair[1])
    )
    frame = pd.DataFrame(
        [
            {
                "Date": b.trade_date,
                "Open": float(b.open),
                "High": float(b.high),
                "Low": float(b.low),
                "Close": float(b.close),
                "Volume": float(b.volume),
            }
            for b in bars
        ]
    )
    frame["Date"] = pd.to_datetime(frame["Date"])
    frame = frame.set_index("Date")
    try:
        parameters = payload.parameters.model_dump()
        adjustment = "前复权(qfq)" if "qfq" in data_source else "数据源未标记复权方式"
        result = run_backtest(
            frame,
            payload.strategy,
            float(payload.cash),
            payload.commission,
            payload.slippage,
            parameters,
            adjustment=adjustment,
        )
    except ValueError as exc:
        raise AppError(422, "BACKTEST_INVALID", str(exc)) from exc
    run = BacktestRun(
        symbol=payload.symbol,
        strategy=payload.strategy,
        date_from=payload.date_from,
        date_to=payload.date_to,
        parameters={
            **parameters,
            "cash": str(payload.cash),
            "commission": payload.commission,
            "slippage": payload.slippage,
            "data_source": data_source,
        },
        metrics=result,
        status="success",
        warning=result["warning"],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {
        "run_id": run.id,
        "symbol": payload.symbol,
        "data_source": data_source,
        "metrics": result,
        **result,
    }


@router.get("/backtests")
def list_backtests(db: Session = Depends(get_db)):
    return db.scalars(select(BacktestRun).order_by(BacktestRun.created_at.desc())).all()


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    return db.scalars(select(SystemJob).order_by(SystemJob.name)).all()


@router.get("/settings/status")
def settings_status():
    settings = get_settings()
    return {
        "database": "configured",
        "akshare": "installed",
        "pandas_ta_classic": "installed",
        "vectorbt": "installed",
        "x": "configured" if settings.x_cookie else "not_configured",
        "llm": "configured"
        if settings.llm_api_key and settings.llm_base_url and settings.llm_model
        else "not_configured",
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model or "not_configured",
        "secrets": {
            "x_cookie": "***" if settings.x_cookie else "",
            "llm_api_key": "***" if settings.llm_api_key else "",
        },
    }


@router.post("/settings/llm/test")
def test_llm_connection():
    try:
        return OpenAICompatibleProvider(get_settings()).test_connection()
    except LLMUnavailableError as exc:
        raise AppError(503, "LLM_CONNECTION_FAILED", str(exc)) from exc
