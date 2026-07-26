from datetime import date, datetime, timedelta
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Account, SystemJob, TradeReview, XPost, XWatchAccount, XWatchQuery
from app.services.reviews import review_metrics
from app.services.technical_snapshots import snapshot_all_holdings
from app.services.data_sources import UnifiedDataService
from app.data_hub.trading_calendar import shanghai_now
from app.watchlist.monitoring import WatchlistMonitoringService


scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
logger = logging.getLogger(__name__)


def _acquire(db, name: str) -> SystemJob | None:
    job = db.scalar(select(SystemJob).where(SystemJob.name == name))
    if (
        job
        and job.status == "running"
        and job.started_at
        and datetime.now() - job.started_at < timedelta(hours=1)
    ):
        return None
    if not job:
        job = SystemJob(name=name, status="pending")
        db.add(job)
    job.status = "running"
    job.started_at = datetime.now()
    job.last_error = None
    db.commit()
    return job


def _fail_job(db, job: SystemJob, exc: Exception, status: str = "failed") -> None:
    job.status = status
    job.last_error = f"{type(exc).__name__}: {exc}"[:1000]
    job.finished_at = datetime.now()
    db.commit()


def generate_review(period: str) -> None:
    with SessionLocal() as db:
        job = _acquire(db, f"review:{period}")
        if not job:
            return
        try:
            today = date.today()
            start = today if period == "daily" else today - timedelta(days=today.weekday())
            end = today if period == "daily" else start + timedelta(days=6)
            count = 0
            for account in db.scalars(select(Account)).all():
                metrics = review_metrics(db, account.id, start, end)
                db.add(
                    TradeReview(
                        period_type=period,
                        period_start=start,
                        conclusion="系统自动汇总；请补充个人复盘结论。",
                        metrics={"account_id": account.id, **metrics},
                    )
                )
                count += 1
            job.status = "success"
            job.result_count = count
            job.finished_at = datetime.now()
            db.commit()
        except Exception as exc:
            _fail_job(db, job, exc)


def sync_x_posts() -> None:
    with SessionLocal() as db:
        job = _acquire(db, "x_sync")
        if not job:
            return
        try:
            queries = [
                item.expression
                for item in db.scalars(select(XWatchQuery).where(XWatchQuery.enabled)).all()
            ]
            queries += [
                f"from:{item.username}"
                for item in db.scalars(select(XWatchAccount).where(XWatchAccount.enabled)).all()
            ]
            if not queries:
                job.status = "paused"
                job.last_error = "尚未配置关注账号或查询表达式"
                job.finished_at = datetime.now()
                db.commit()
                return
            posts = (
                UnifiedDataService(db)
                .collect_social_queries(queries, limit=20)
                .require_value()
            )
            count = 0
            for post in posts:
                exists = db.scalar(
                    select(XPost.id).where(
                        XPost.platform == "x", XPost.post_id == post["post_id"]
                    )
                )
                if not exists:
                    db.add(XPost(platform="x", **post))
                    count += 1
            job.status = "success"
            job.result_count = count
            job.finished_at = datetime.now()
            db.commit()
        except Exception as exc:
            _fail_job(db, job, exc, status="paused")


def generate_technical_snapshots() -> None:
    with SessionLocal() as db:
        job = _acquire(db, "technical_snapshots")
        if not job:
            return
        try:
            result = snapshot_all_holdings(db, refresh_market=True)
            job.status = "success" if not result["errors"] else "partial"
            job.result_count = result["processed"]
            job.last_error = str(result["errors"][:5]) if result["errors"] else None
            job.finished_at = datetime.now()
            db.commit()
        except Exception as exc:
            _fail_job(db, job, exc)


def scan_watchlist() -> None:
    try:
        with SessionLocal() as db:
            WatchlistMonitoringService(db).scan(now=shanghai_now())
    except Exception:
        logger.exception("scheduled watchlist scan failed")


def start_scheduler() -> None:
    settings = get_settings()
    if scheduler.running or not settings.scheduler_enabled:
        return
    scheduler.add_job(
        generate_review,
        "cron",
        args=["daily"],
        day_of_week="mon-fri",
        hour=15,
        minute=30,
        id="daily_review",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        generate_technical_snapshots,
        "cron",
        day_of_week="mon-fri",
        hour=16,
        minute=10,
        id="technical_snapshots",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        generate_review,
        "cron",
        args=["weekly"],
        day_of_week="sun",
        hour=20,
        minute=0,
        id="weekly_review",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.x_cookie:
        scheduler.add_job(
            sync_x_posts,
            "interval",
            minutes=settings.x_sync_minutes,
            id="x_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.watchlist_monitor_enabled:
        try:
            scheduler.add_job(
                scan_watchlist,
                "interval",
                seconds=settings.watchlist_monitor_interval_seconds,
                id="watchlist_monitor",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        except Exception:
            logger.exception("watchlist scheduler registration failed")
    try:
        scheduler.start()
    except Exception:
        logger.exception("scheduler startup failed; web application will continue")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
