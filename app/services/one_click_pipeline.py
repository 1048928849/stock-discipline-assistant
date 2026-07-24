from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.router import DataHubRouter
from app.data_hub.trading_calendar import get_trading_calendar
from app.data_hub.quality import observation_is_stale, policy_for
from app.domain.package_builder import build_decision_package
from app.errors import AppError
from app.models import (
    Account,
    CompanyAnnouncement,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchEvidence,
    CompanyResearchRefresh,
    CompanyValuationSnapshot,
    DataQualityRecord,
    MarketDailyBar,
    MarketQuote,
    PlanAnalysisRun,
    TradePlan,
)
from app.research.orchestrator import ExistingAIResearchOrchestrator
from app.schemas_workflow import (
    OneClickPlanRequest,
    TradePlanAIRequest,
    TradePlanPreviewRequest,
    TradePlanSaveRequest,
)
from app.services.trade_plan_ai import run_ai_analysis
from app.services.company_research import refresh_company_research_if_needed
from app.services.data_sources import build_data_hub
from app.services.plan_freeze import freeze_trade_plan
from app.services.trade_plan_generator import (
    GENERATOR_PARAMETERS,
    generate_trade_plan_preview,
)


logger = logging.getLogger(__name__)


def _step(code: str, name: str, status: str, detail: str, **extra) -> dict:
    return {
        "code": code,
        "name": name,
        "status": status,
        "detail": detail,
        "fallback_used": bool(extra.pop("fallback_used", False)),
        "missing": extra.pop("missing", []),
        **extra,
    }


def _default_account(db: Session, payload: OneClickPlanRequest) -> tuple[Account, bool]:
    if payload.account_id is not None:
        account = db.get(Account, payload.account_id)
        if account is None:
            raise AppError(404, "ACCOUNT_NOT_FOUND", "所选账户不存在，请重新选择")
        return account, False
    account = db.scalar(select(Account).order_by(Account.updated_at.desc(), Account.id.desc()))
    if account:
        return account, False
    capital = payload.plan_capital or Decimal(str(GENERATOR_PARAMETERS["default_account_equity"]))
    available = payload.available_cash if payload.available_cash is not None else capital
    account = Account(
        name="默认研究账户",
        total_assets=capital,
        cash=available,
        available_cash=available,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account, True


def _latest_bar(db: Session, symbol: str) -> MarketDailyBar | None:
    return db.scalar(
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == symbol)
        .order_by(MarketDailyBar.trade_date.desc(), MarketDailyBar.fetched_at.desc())
    )


def _sync_stock(
    db: Session, symbol: str, provider: DataHubRouter, refresh: bool
) -> tuple[dict, dict]:
    cached = _latest_bar(db, symbol)
    stale = cached is None or get_trading_calendar().session_lag(cached.trade_date) > 0
    if cached and not stale and not refresh:
        quote_step = _cached_quote_step(db, symbol)
        record = _latest_quality_record(db, symbol, "market.daily.qfq")
        cached_quality = (
            record.quality_status
            if record and not record.persisted
            else cached.quality_status
        )
        return (
            _step(
                "market_data",
                "行情数据",
                "success",
                f"本地前复权日线有效，截止 {cached.trade_date.isoformat()}。",
                source=cached.source,
                data_time=cached.fetched_at.isoformat(),
                observed_at=cached.trade_date.isoformat(),
                quality_status=cached_quality,
            ),
            {
                "data_date": cached.trade_date.isoformat(),
                "source": cached.source,
                "quote_step": quote_step,
            },
        )
    try:
        history_result = provider.get_history(
            symbol, date.today() - timedelta(days=900), date.today()
        )
        if history_result.quality_status.blocks_execution:
            db.commit()
        bars = history_result.require_trusted_value()
        if len(bars) < 80:
            raise ProviderUnavailableError(f"前复权日线仅有 {len(bars)} 根，少于80根")
        quote_error = None
        quote_result = None
        try:
            quote_result = provider.get_quote(symbol)
            quote = quote_result.require_trusted_value()
        except ProviderUnavailableError as exc:
            if quote_result and quote_result.quality_status.blocks_execution:
                db.commit()
            quote_error = str(exc)
            latest = bars[-1]
            old_quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
            from app.data_hub.contracts import Quote

            quote = Quote(
                symbol=symbol,
                name=old_quote.name if old_quote and old_quote.name else symbol,
                price=latest.close,
                quote_type="latest_close",
                observed_at=latest.observed_at,
                price_unit=latest.price_unit,
                source=latest.source,
                source_api="history_latest_close",
                fetched_at=latest.fetched_at,
            )
        for bar in bars:
            item = db.scalar(
                select(MarketDailyBar).where(
                    MarketDailyBar.symbol == symbol,
                    MarketDailyBar.trade_date == bar.trade_date,
                    MarketDailyBar.source == bar.source,
                )
            )
            if item is None:
                db.add(
                    MarketDailyBar(
                        **bar.__dict__,
                        quality_status=history_result.quality_status.value,
                        quality_record_id=history_result.quality_record_id,
                    )
                )
            else:
                for key in (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "adjustment",
                    "price_unit",
                    "volume_unit",
                    "observed_at",
                    "fetched_at",
                ):
                    setattr(item, key, getattr(bar, key))
                item.quality_status = history_result.quality_status.value
                item.quality_record_id = history_result.quality_record_id
        stored_quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
        if quote_result and not quote_result.quality_status.blocks_execution and stored_quote is None:
            stored_quote = MarketQuote(
                symbol=symbol,
                name=quote.name,
                price=quote.price,
                quote_type=quote.quote_type,
                observed_at=quote.observed_at,
                price_unit=quote.price_unit,
                quality_status=(
                    quote_result.quality_status.value if quote_result else "SINGLE_SOURCE"
                ),
                quality_record_id=quote_result.quality_record_id if quote_result else None,
                source=quote.source,
                source_api=quote.source_api,
                fetched_at=quote.fetched_at,
            )
            db.add(stored_quote)
        elif quote_result and not quote_result.quality_status.blocks_execution and stored_quote:
            stored_quote.name = quote.name
            stored_quote.price = quote.price
            stored_quote.quote_type = quote.quote_type
            stored_quote.observed_at = quote.observed_at
            stored_quote.price_unit = quote.price_unit
            stored_quote.quality_status = (
                quote_result.quality_status.value if quote_result else "SINGLE_SOURCE"
            )
            stored_quote.quality_record_id = (
                quote_result.quality_record_id if quote_result else None
            )
            stored_quote.source = quote.source
            stored_quote.source_api = quote.source_api
            stored_quote.fetched_at = quote.fetched_at
        provider.mark_persisted(history_result)
        if quote_result and not quote_result.quality_status.blocks_execution:
            provider.mark_persisted(quote_result)
        db.commit()
        quote_step = _step(
            "market_quote",
            "当前价格质量",
            "success"
            if quote_result and not quote_result.quality_status.blocks_execution
            else "partial",
            "已取得实时价格。" if quote_result else "实时价格不可用，明确降级为最新收盘价。",
            source=quote.source,
            observed_at=quote.observed_at.isoformat(),
            fetched_at=quote.fetched_at.isoformat(),
            data_time=quote.observed_at.isoformat(),
            quality_status=(
                quote_result.quality_status.value if quote_result else "MISSING"
            ),
            provider_observations=(
                quote_result.provider_observations if quote_result else []
            ),
            conflict_fields=quote_result.conflict_fields if quote_result else [],
            quote_type=quote.quote_type,
            price=str(quote.price),
            fallback_used=quote_result is None,
        )
        return (
            _step(
                "market_data",
                "行情数据",
                "success",
                f"已自动同步 {len(bars)} 根前复权日线，截止 {bars[-1].trade_date.isoformat()}。"
                + (f" 实时行情失败，使用最新收盘：{quote_error}" if quote_error else ""),
                fallback_used=bool(quote_error) or history_result.fallback_used,
                quality_status=history_result.quality_status.value,
                observed_at=history_result.observed_at.isoformat()
                if history_result.observed_at
                else None,
                fetched_at=history_result.fetched_at.isoformat(),
                provider_observations=history_result.provider_observations,
                source=bars[-1].source,
                data_time=bars[-1].fetched_at.isoformat(),
            ),
            {
                "data_date": bars[-1].trade_date.isoformat(),
                "source": bars[-1].source,
                "quote_step": quote_step,
            },
        )
    except Exception as exc:
        db.rollback()
        cached = _latest_bar(db, symbol)
        if cached:
            quote_step = _cached_quote_step(db, symbol)
            return (
                _step(
                    "market_data",
                    "行情数据",
                    "partial",
                    f"外部行情同步失败，回退到 {cached.trade_date.isoformat()} 的缓存：{exc}",
                    fallback_used=True,
                    source=cached.source,
                    data_time=cached.fetched_at.isoformat(),
                    quality_status=(
                        "CONFLICTED"
                        if "quality is CONFLICTED" in str(exc)
                        else cached.quality_status
                    ),
                    missing=["最新行情"],
                ),
                {
                    "data_date": cached.trade_date.isoformat(),
                    "source": cached.source,
                    "quote_step": quote_step,
                },
            )
        return (
            _step(
                "market_data",
                "行情数据",
                "failed",
                f"行情同步失败且没有可用缓存：{exc}",
                missing=["前复权日线", "当前价格"],
            ),
            {
                "data_date": None,
                "source": "数据不足",
                "quote_step": _cached_quote_step(db, symbol),
            },
        )


def _series_assessment(rows: list[dict]) -> dict:
    frame = pd.DataFrame(rows).sort_values("date")
    close = frame["close"].astype(float)
    if len(close) < 60:
        return {"state": "无法判断", "risk": "无法判断", "return_20d": None}
    current = float(close.iloc[-1])
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])
    return_20d = (current / float(close.iloc[-21]) - 1) * 100
    drawdown_20d = (current / float(close.tail(20).max()) - 1) * 100
    state = (
        "上升"
        if current > ma20 > ma60 and return_20d > 0
        else "下降"
        if current < ma20 < ma60 and return_20d < -3
        else "震荡"
    )
    risk = "高" if state == "下降" or drawdown_20d <= -8 else "低" if state == "上升" else "中等"
    return {
        "state": state,
        "risk": risk,
        "close": round(current, 4),
        "ma20": round(ma20, 4),
        "ma60": round(ma60, 4),
        "return_20d": round(return_20d, 2),
        "drawdown_20d": round(drawdown_20d, 2),
    }


def _market_assessment(db: Session, provider: DataHubRouter) -> tuple[dict, dict]:
    cache_symbol = "CSI000300"
    cached = db.scalars(
        select(MarketDailyBar)
        .where(MarketDailyBar.symbol == cache_symbol)
        .order_by(MarketDailyBar.trade_date)
    ).all()
    if cached and get_trading_calendar().session_lag(cached[-1].trade_date) == 0:
        rows = [
            {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
            for item in cached
        ]
        assessment = _series_assessment(rows)
        detail = (
            f"沪深300：20日涨跌 {assessment['return_20d']}%，收盘 {assessment.get('close')}，"
            f"MA20 {assessment.get('ma20')}，MA60 {assessment.get('ma60')}；市场风险 {assessment['risk']}。"
        )
        return assessment, _step(
            "market_judgement",
            "市场判断",
            "success",
            detail,
            source=cached[-1].source,
            data_time=cached[-1].fetched_at.isoformat(),
            observed_at=cached[-1].trade_date.isoformat(),
        )
    try:
        history_result = provider.get_index_history(
            "csi000300", date.today() - timedelta(days=240), date.today()
        )
        history = history_result.require_value()
        for row in history["rows"]:
            existing = db.scalar(
                select(MarketDailyBar).where(
                    MarketDailyBar.symbol == cache_symbol,
                    MarketDailyBar.trade_date == row["date"],
                    MarketDailyBar.source == history["source"],
                )
            )
            if existing is None:
                close = Decimal(str(row["close"]))
                db.add(
                    MarketDailyBar(
                        symbol=cache_symbol,
                        trade_date=row["date"],
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=Decimal(str(row.get("volume") or 0)),
                        adjustment="unadjusted",
                        price_unit="CNY",
                        volume_unit="share",
                        observed_at=datetime.combine(row["date"], datetime.min.time()),
                        quality_status=history_result.quality_status.value,
                        quality_record_id=history_result.quality_record_id,
                        source=history["source"],
                        fetched_at=history["fetched_at"],
                    )
                )
        provider.mark_persisted(history_result)
        db.commit()
        assessment = _series_assessment(history["rows"])
        detail = (
            f"沪深300：20日涨跌 {assessment['return_20d']}%，"
            f"收盘 {assessment.get('close')}，MA20 {assessment.get('ma20')}，"
            f"MA60 {assessment.get('ma60')}；市场风险 {assessment['risk']}。"
        )
        return assessment, _step(
            "market_judgement",
            "市场判断",
            "success" if assessment["state"] != "无法判断" else "partial",
            detail,
            source=history["source"],
            data_time=history["fetched_at"].isoformat(),
            quality_status=history_result.quality_status.value,
            observed_at=history_result.observed_at.isoformat()
            if history_result.observed_at
            else None,
            fetched_at=history_result.fetched_at.isoformat(),
            provider_observations=history_result.provider_observations,
        )
    except Exception as exc:
        db.rollback()
        if cached:
            rows = [
                {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
                for item in cached
            ]
            assessment = _series_assessment(rows)
            return assessment, _step(
                "market_judgement",
                "市场判断",
                "partial",
                f"指数刷新失败，回退到 {cached[-1].trade_date.isoformat()} 的缓存：{exc}",
                fallback_used=True,
                source=cached[-1].source,
                data_time=cached[-1].fetched_at.isoformat(),
                missing=["最新沪深300行情"],
            )
        assessment = {"state": "无法判断", "risk": "无法判断", "return_20d": None}
        return assessment, _step(
            "market_judgement",
            "市场判断",
            "partial",
            f"宽基指数获取失败，市场环境不作猜测：{exc}",
            missing=["沪深300近60个交易日"],
        )


def _pick(raw: dict, *keys):
    return next((raw.get(key) for key in keys if raw.get(key) not in (None, "")), None)


def _ensure_profile(
    db: Session, symbol: str, provider: DataHubRouter
) -> tuple[CompanyProfile | None, dict]:
    profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
    if profile and profile.industry:
        record = _latest_quality_record(db, symbol, "fundamental.profile")
        profile_quality = (
            record.quality_status
            if record and not record.persisted
            else "STALE"
            if observation_is_stale(
                record.observed_at if record and record.observed_at else profile.fetched_at,
                policy_for("fundamental.profile"),
            )
            else record.quality_status
            if record
            else "SINGLE_SOURCE"
        )
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "success",
            f"{profile.name}，所属行业：{profile.industry}。",
            source=profile.source,
            data_time=profile.fetched_at.isoformat(),
            observed_at=profile.fetched_at.isoformat(),
            quality_status=profile_quality,
            missing=["概念板块自动映射", "完整产业链节点"] if not profile.raw_data else [],
        )
    try:
        profile_result = provider.company_profile(symbol)
        raw = profile_result.require_value()
        now = datetime.now()
        values = {
            "name": str(_pick(raw, "A股简称", "公司名称") or symbol),
            "industry": _pick(raw, "细分行业", "所属行业"),
            "market": _pick(raw, "所属市场"),
            "main_business": _pick(raw, "主营业务"),
            "business_scope": _pick(raw, "经营范围"),
            "website": _pick(raw, "官方网站"),
            "source": "巨潮资讯公司概况（AKShare）",
            "source_url": "http://www.cninfo.com.cn/new/commonUrl?url=data/stock/stockDetail",
            "raw_data": raw,
            "fetched_at": now,
        }
        if profile is None:
            profile = CompanyProfile(symbol=symbol, **values)
            db.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
        provider.mark_persisted(profile_result, cached_at=now)
        db.commit()
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "success" if profile.industry else "partial",
            f"识别为 {profile.name}；行业：{profile.industry or '暂无可靠数据'}。",
            source=profile.source,
            data_time=now.isoformat(),
            observed_at=profile_result.observed_at.isoformat()
            if profile_result.observed_at
            else None,
            fetched_at=profile_result.fetched_at.isoformat(),
            quality_status=profile_result.quality_status.value,
            provider_observations=profile_result.provider_observations,
            missing=[] if profile.industry else ["所属行业", "概念板块", "产业链节点"],
        )
    except Exception as exc:
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "partial",
            f"公司行业识别失败，保留已有信息：{exc}",
            fallback_used=profile is not None,
            missing=["所属行业", "概念板块", "产业链节点"],
        )


def _sector_board_name(profile: CompanyProfile) -> str:
    text = f"{profile.industry or ''} {profile.main_business or ''}"
    aliases = (
        (("光模块", "光通信", "通信设备"), "通信设备"),
        (("半导体", "集成电路", "芯片"), "半导体"),
        (("软件", "信息技术服务"), "软件开发"),
        (("银行",), "银行"),
        (("证券",), "证券"),
        (("保险",), "保险"),
        (("汽车",), "汽车整车"),
        (("白酒",), "酿酒行业"),
        (("电池", "锂电"), "电池"),
        (("光伏",), "光伏设备"),
        (("医药", "制药"), "化学制药"),
    )
    return next((board for keys, board in aliases if any(key in text for key in keys)), profile.industry)


def _sector_assessment(
    db: Session,
    provider: DataHubRouter,
    profile: CompanyProfile | None,
    market: dict,
    refresh: bool = False,
) -> tuple[dict, dict]:
    result: dict[str, object]
    if profile is None or not profile.industry:
        result = {"state": "无法判断", "relative_20d": None, "is_mainline": None}
        return result, _step(
            "industry_judgement",
            "行业判断",
            "partial",
            "缺少可靠所属行业，无法计算行业相对强弱。",
            missing=["所属行业", "行业指数"],
        )
    if not refresh:
        previous = db.scalar(
            select(PlanAnalysisRun)
            .where(
                PlanAnalysisRun.symbol == profile.symbol,
                PlanAnalysisRun.status.in_(("success", "confirmed")),
                PlanAnalysisRun.created_at >= datetime.now() - timedelta(hours=24),
            )
            .order_by(PlanAnalysisRun.created_at.desc())
        )
        previous_result = previous.result_snapshot if previous else None
        cached_assessment = (previous_result or {}).get("plan", {}).get("industry_assessment")
        cached_step = next(
            (
                item
                for item in (previous_result or {}).get("steps", [])
                if item.get("code") == "industry_judgement" and item.get("status") == "success"
            ),
            None,
        )
        if cached_assessment and cached_step:
            prefix = "使用24小时内行业判断缓存；"
            detail = cached_step["detail"]
            while detail.startswith(prefix + prefix):
                detail = detail[len(prefix) :]
            return cached_assessment, {
                **cached_step,
                "detail": detail if detail.startswith(prefix) else f"{prefix}{detail}",
                "fallback_used": True,
            }
    try:
        board_name = _sector_board_name(profile)
        history_result = provider.get_sector_history(
            board_name, date.today() - timedelta(days=240), date.today()
        )
        history = history_result.require_value()
        assessment = _series_assessment(history["rows"])
        market_return = market.get("return_20d")
        relative = (
            assessment["return_20d"] - market_return
            if assessment["return_20d"] is not None and market_return is not None
            else None
        )
        state = (
            "强"
            if relative is not None and relative >= 3 and assessment["state"] == "上升"
            else "弱"
            if relative is not None and relative <= -3 and assessment["state"] == "下降"
            else "中性"
        )
        result = {
            "state": state,
            "industry": profile.industry,
            "board_name": board_name,
            "return_20d": assessment["return_20d"],
            "relative_20d": round(relative, 2) if relative is not None else None,
            "is_mainline": bool(state == "强"),
            "mainline_method": "行业20日涨跌相对沪深300的规则代理，不等同于完整资金主线模型",
        }
        return result, _step(
            "industry_judgement",
            "行业判断",
            "success",
            f"{profile.industry}（行情代理板块：{board_name}）20日涨跌 {assessment['return_20d']}%，"
            f"相对沪深300 {result['relative_20d']}个百分点，判定为{state}。",
            source=history["source"],
            data_time=history["fetched_at"].isoformat(),
            quality_status=history_result.quality_status.value,
            observed_at=history_result.observed_at.isoformat()
            if history_result.observed_at
            else None,
            fetched_at=history_result.fetched_at.isoformat(),
            provider_observations=history_result.provider_observations,
        )
    except Exception as exc:
        result = {"state": "无法判断", "relative_20d": None, "is_mainline": None}
        return result, _step(
            "industry_judgement",
            "行业判断",
            "partial",
            f"已识别行业“{profile.industry}”，但行业指数获取失败：{exc}",
            missing=["行业指数近60个交易日", "行业相对强度"],
        )


def _research_inventory(db: Session, symbol: str) -> tuple[dict, dict]:
    financials = db.scalars(
        select(CompanyFinancialPeriod).where(CompanyFinancialPeriod.symbol == symbol)
    ).all()
    announcements = db.scalars(
        select(CompanyAnnouncement).where(CompanyAnnouncement.symbol == symbol)
    ).all()
    evidence = db.scalars(
        select(CompanyResearchEvidence).where(CompanyResearchEvidence.symbol == symbol)
    ).all()
    valuation = db.scalar(
        select(CompanyValuationSnapshot)
        .where(CompanyValuationSnapshot.symbol == symbol)
        .order_by(CompanyValuationSnapshot.trade_date.desc())
    )
    announcement_scan = db.scalar(
        select(CompanyResearchRefresh).where(
            CompanyResearchRefresh.symbol == symbol,
            CompanyResearchRefresh.section == "announcements",
        )
    )
    risks = [item for item in announcements if item.risk_level in {"红", "黄"}]
    missing = []
    if not financials:
        missing.append("最近12季度财务数据")
    if not announcement_scan or not announcement_scan.last_success_at:
        missing.append("公司公告目录")
    if not evidence:
        missing.append("产业与公开信息证据")
    if not valuation:
        missing.append("估值数据")
    result = {
        "financial_periods": len(financials),
        "announcements": len(announcements),
        "announcement_scan_completed": bool(
            announcement_scan and announcement_scan.last_success_at
        ),
        "risk_events": [
            {
                "title": item.title,
                "risk_level": item.risk_level,
                "published_date": item.published_date.isoformat(),
                "source": item.catalog_source,
                "url": item.source_document_url or item.url,
            }
            for item in sorted(risks, key=lambda x: x.published_date, reverse=True)[:10]
        ],
        "evidence_count": len(evidence),
        "valuation_available": valuation is not None,
        "missing": missing,
    }
    return result, _step(
        "company_risk",
        "公司风险与公开信息",
        "success" if not missing else "partial",
        f"本地已有财务期数 {len(financials)}、公告 {len(announcements)}、产业证据 {len(evidence)}。",
        missing=missing,
        source="本地公司研究中心（原始来源保留在证据记录）",
        data_time=datetime.now().isoformat(),
        observed_at=announcement_scan.checked_at.isoformat()
        if announcement_scan and announcement_scan.checked_at
        else None,
        quality_status=(
            announcement_scan.quality_status
            if announcement_scan and announcement_scan.quality_status
            else "STALE"
            if announcement_scan
            and announcement_scan.checked_at
            and observation_is_stale(
                announcement_scan.checked_at,
                policy_for("announcement.catalog"),
            )
            else "MISSING"
        ),
        required_missing=(
            []
            if announcement_scan
            and announcement_scan.quality_status in {"VERIFIED", "SINGLE_SOURCE"}
            else ["announcements"]
        ),
        optional_missing=[
            item
            for item, available in (
                ("financials", bool(financials)),
                ("valuation", valuation is not None),
                ("industry_evidence", bool(evidence)),
            )
            if not available
        ],
    )


def _latest_quality_record(
    db: Session, symbol: str, capability: str
) -> DataQualityRecord | None:
    return db.scalar(
        select(DataQualityRecord)
        .where(
            DataQualityRecord.symbol == symbol,
            DataQualityRecord.capability == capability,
        )
        .order_by(DataQualityRecord.id.desc())
    )


def _cached_quote_step(db: Session, symbol: str) -> dict:
    quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
    record = _latest_quality_record(db, symbol, "market.quote.realtime")
    quality = (
        record.quality_status
        if record and not record.persisted
        else quote.quality_status
        if quote
        else "MISSING"
    )
    return _step(
        "market_quote",
        "当前价格质量",
        "success" if quality in {"VERIFIED", "SINGLE_SOURCE"} else "partial",
        "已读取持久化行情质量。" if quote else "没有可用的当前价格。",
        source=quote.source if quote else "none",
        observed_at=quote.observed_at.isoformat() if quote else None,
        fetched_at=quote.fetched_at.isoformat() if quote else None,
        data_time=quote.observed_at.isoformat() if quote else None,
        quality_status=quality,
        provider_observations=record.provider_observations if record else [],
        conflict_fields=record.conflict_fields if record else [],
        quote_type=quote.quote_type if quote else None,
        price=str(quote.price) if quote else None,
        fallback_used=bool(quote and quote.quote_type != "realtime"),
    )


def _decision(preview: dict, position_mode: str) -> dict:
    holding = preview["existing_position"]
    if position_mode == "持仓":
        if holding["hard_stop_triggered"] or preview.get("pattern", {}).get("platform_broken"):
            status, label = "PLAN_INVALID_EXIT", "计划失效，需要退出"
        elif holding["first_reduction_triggered"]:
            status, label = "REDUCE", "建议减仓"
        elif holding["confirmation_add_allowed"]:
            status, label = "CONDITIONAL_ADD", "允许条件式加仓"
        elif preview["status"] == "NO_TRADE":
            status, label = "REDUCE", "建议减仓"
        else:
            status, label = "HOLD", "允许持有"
    elif preview["status"] == "READY" and preview["current_buy_allowed"]:
        status, label = "TRIAL_ALLOWED", "允许试仓"
    elif preview["status"] == "NO_TRADE":
        status, label = "BUY_PROHIBITED", "禁止买入"
    else:
        status, label = "WAIT", "等待观察"
    return {
        "status": status,
        "label": label,
        "next_action": preview["next_observations"][0]
        if preview["next_observations"]
        else "等待下一次有效触发并重新分析。",
        "rule_authority": "最终状态、仓位、止损和加仓均由确定性规则引擎决定，AI无权修改。",
    }


def run_one_click_analysis(db: Session, payload: OneClickPlanRequest) -> dict:
    account, created_account = _default_account(db, payload)
    run = PlanAnalysisRun(
        symbol=payload.symbol,
        account_id=account.id,
        position_mode=payload.position_mode,
        status="running",
        request_snapshot=payload.model_dump(mode="json"),
        pipeline_steps=[],
        result_snapshot=None,
        user_confirmed=False,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    provider = build_data_hub(db)
    research_orchestrator = ExistingAIResearchOrchestrator(
        lambda request: run_ai_analysis(db, request)
    )
    steps = [
        _step(
            "account",
            "账户与持仓",
            "success",
            f"使用账户“{account.name}”，权益 {float(account.total_assets):.2f} 元。"
            + (" 已自动建立默认研究账户。" if created_account else ""),
            source="本地账户设置",
            data_time=datetime.now().isoformat(),
        )
    ]
    try:
        stock_step, stock_meta = _sync_stock(db, payload.symbol, provider, payload.refresh)
        steps.append(stock_step)
        steps.append(stock_meta.get("quote_step") or _cached_quote_step(db, payload.symbol))
        research_refresh = refresh_company_research_if_needed(
            db,
            payload.symbol,
            force=payload.refresh,
            include_documents=False,
            data_service=provider,
        )
        refresh_sections = research_refresh.get("sections", {})
        refresh_failed = [
            name
            for name, item in refresh_sections.items()
            if item.get("status") in {"unavailable", "failed"}
        ]
        refresh_cached = [
            name
            for name, item in refresh_sections.items()
            if item.get("status") == "cache_fallback"
        ]
        steps.append(
            _step(
                "company_research_refresh",
                "公司研究刷新",
                "success"
                if not refresh_failed and not research_refresh.get("missing_data")
                else "partial",
                (
                    f"已检查公司概况、最近12季度财务、估值、公告和风险事件；"
                    f"实际刷新：{'、'.join(research_refresh.get('refreshed_sections') or ['无需刷新'])}。"
                    + (f" 使用缓存：{'、'.join(refresh_cached)}。" if refresh_cached else "")
                ),
                fallback_used=bool(refresh_cached),
                missing=research_refresh.get("missing_data", []),
                source="统一数据服务",
                data_time=research_refresh.get("updated_at"),
                provider_status=provider.provider_status(),
                freshness=research_refresh.get("freshness", []),
            )
        )
        profile, profile_step = _ensure_profile(db, payload.symbol, provider)
        steps.append(profile_step)
        market, market_step = _market_assessment(db, provider)
        steps.append(market_step)
        sector, sector_step = _sector_assessment(db, provider, profile, market, payload.refresh)
        steps.append(sector_step)
        research, research_step = _research_inventory(db, payload.symbol)
        research["refresh"] = research_refresh
        research["provider_status"] = provider.provider_status()
        steps.append(research_step)

        effective_total_cap = payload.max_total_position_pct
        if market["risk"] == "高":
            effective_total_cap = min(
                effective_total_cap,
                Decimal(str(GENERATOR_PARAMETERS["market_high_risk_total_cap_pct"])),
            )
        elif market["risk"] == "中等":
            effective_total_cap = min(
                effective_total_cap,
                Decimal(str(GENERATOR_PARAMETERS["market_neutral_total_cap_pct"])),
            )
        generator_request = TradePlanPreviewRequest(
            symbol=payload.symbol,
            account_id=account.id,
            risk_pct=payload.risk_pct,
            max_position_pct=payload.max_position_pct,
            max_total_position_pct=effective_total_cap,
            max_industry_position_pct=payload.max_industry_position_pct,
            market_state=market["state"],
            sector_state=sector["state"],
            logic_invalidation=payload.logic_invalidation,
            position_mode=payload.position_mode,
            holding_quantity=payload.holding_quantity,
            holding_cost_price=payload.holding_cost_price,
            market_evidence=market_step["detail"],
            market_source=market_step.get("source", "数据不足"),
            market_data_time=market_step.get("data_time", datetime.now().isoformat()),
            sector_evidence=sector_step["detail"],
            sector_source=sector_step.get("source", "数据不足"),
            sector_data_time=sector_step.get("data_time", datetime.now().isoformat()),
        )
        preview = generate_trade_plan_preview(db, generator_request)
        decision = _decision(preview, payload.position_mode)
        preview["decision"] = decision
        preview["market_assessment"] = market
        preview["industry_assessment"] = sector
        preview["research_inventory"] = research
        preview["required_research_capabilities"] = (
            payload.required_research_capabilities
        )
        steps.extend(
            [
                _step(
                    "stock_analysis",
                    "个股分析",
                    "success" if preview["pattern"] else "partial",
                    "已完成多周期、平台、均线、量能、MACD、RSI、ATR及关键结构计算。"
                    if preview["pattern"]
                    else "个股行情不足，技术结构无法完整计算。",
                    missing=[] if preview["pattern"] else ["至少80根前复权日线"],
                    source=stock_meta["source"],
                    data_time=preview.get("data_date"),
                ),
                _step(
                    "risk_calculation",
                    "风险计算",
                    "success" if preview["position_calculation"] else "partial",
                    preview["position_calculation"].get(
                        "formula", "买入触发价或止损不足，无法计算仓位。"
                    ),
                    missing=[]
                    if preview["position_calculation"]
                    else ["有效买入触发价", "硬止损价"],
                    source="确定性规则引擎 + 本地账户",
                    data_time=datetime.now().isoformat(),
                ),
            ]
        )
        ai_result = None
        ai_id = None
        if payload.enable_ai:
            ai_request = TradePlanAIRequest(
                **generator_request.model_dump(), preview_hash=preview["preview_hash"]
            )
            research_execution = research_orchestrator.run(ai_request)
            ai_result = research_execution.legacy_payload
            ai_id = ai_result.get("id")
            steps.append(
                _step(
                    "ai_explanation",
                    "AI解释",
                    "success" if ai_result.get("status") == "success" else "partial",
                    "AI已基于证据包生成解释；不改变规则结论。"
                    if ai_result.get("status") == "success"
                    else ai_result.get("error", "AI未配置或暂不可用，规则计划不受影响。"),
                    fallback_used=ai_result.get("status") != "success",
                    missing=[]
                    if ai_result.get("status") == "success"
                    else ["AI辅助解释"],
                    source=f"{ai_result.get('provider', '未配置')}/{ai_result.get('model', '未配置')}",
                    data_time=ai_result.get("created_at", datetime.now().isoformat()),
                )
            )
        else:
            ai_result = {
                "status": "skipped",
                "error": "用户关闭了AI辅助分析；规则计划已独立完成。",
                "result": None,
            }
            steps.append(
                _step(
                    "ai_explanation",
                    "AI解释",
                    "skipped",
                    "用户在高级设置中关闭了AI；规则计划已完整生成。",
                    missing=["AI辅助解释"],
                )
            )
        decision_package = build_decision_package(
            preview=preview,
            decision=decision,
            steps=steps,
            ai_result=ai_result,
            orchestrator_id=research_orchestrator.orchestrator_id,
        )
        if decision_package.quality_status.blocks_execution and preview["status"] == "READY":
            preview = deepcopy(preview)
            preview["deterministic_rule_status"] = "READY"
            preview["status"] = "WAIT"
            preview["current_buy_allowed"] = False
            preview["buy_plan"]["allowed"] = False
            preview["confirmation_add"]["allowed"] = False
            decision = {
                **decision,
                "status": decision_package.strategy_decision.decision_code,
                "label": decision_package.strategy_decision.label,
                "next_action": decision_package.strategy_decision.next_action,
            }
            preview["decision"] = decision
        steps.append(
            _step(
                "plan_output",
                "生成计划",
                "success",
                f"确定性结论：{decision['label']}。等待用户确认后保存正式版本。",
                source=f"规则 {preview['rule']['version']}",
                data_time=preview["generated_at"],
            )
        )
        result = {
            "run_id": run.id,
            "status": "success",
            "account": {
                "id": account.id,
                "name": account.name,
                "auto_created": created_account,
            },
            "steps": steps,
            "decision": decision,
            "plan": preview,
            "ai": ai_result,
            "decision_package": decision_package.model_dump(mode="json"),
            "generator_request": generator_request.model_dump(mode="json"),
            "can_save": decision_package.freeze_allowed,
            "save_disabled_reason": (
                None
                if decision_package.freeze_allowed
                else "；".join(decision_package.blocked_reasons)
            ),
        }
        run.status = "success"
        run.pipeline_steps = steps
        run.result_snapshot = result
        run.ai_analysis_id = ai_id
        db.commit()
        return result
    except Exception as exc:
        logger.exception("one-click analysis failed", extra={"symbol": payload.symbol})
        db.rollback()
        run = db.get(PlanAnalysisRun, run.id)
        if run:
            run.status = "failed"
            run.pipeline_steps = steps
            run.error = f"{type(exc).__name__}: {exc}"
            db.commit()
        raise


def confirm_one_click_plan(db: Session, run_id: int) -> dict:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    run = db.scalar(
        select(PlanAnalysisRun).where(PlanAnalysisRun.id == run_id).with_for_update()
    )
    if run is None:
        raise AppError(404, "ANALYSIS_RUN_NOT_FOUND", "一键分析记录不存在")
    if run.confirmed_plan_id:
        raise AppError(409, "ANALYSIS_ALREADY_CONFIRMED", "该分析已经保存为正式计划")
    result = run.result_snapshot or {}
    plan = result.get("plan") or {}
    decision_package = result.get("decision_package")
    if not decision_package:
        raise AppError(
            422,
            "DECISION_PACKAGE_REQUIRED",
            "Legacy analysis cannot be confirmed; run a new analysis.",
        )
    if not decision_package.get("freeze_allowed"):
        reasons = "；".join(decision_package.get("blocked_reasons") or [])
        raise AppError(
            422,
            "ANALYSIS_QUALITY_BLOCKED",
            reasons or "数据质量不允许冻结正式计划",
        )
    generator_payload = result.get("generator_request")
    if not generator_payload or not plan.get("preview_hash"):
        raise AppError(422, "ANALYSIS_NOT_SAVABLE", "分析没有形成可保存的规则快照")
    save_request = TradePlanSaveRequest(
        **generator_payload,
        preview_hash=plan["preview_hash"],
        ai_analysis_id=run.ai_analysis_id,
    )
    try:
        saved = freeze_trade_plan(
            db,
            request=save_request,
            decision_package=decision_package,
            analysis_created_at=run.created_at,
            analysis_run_id=run.id,
        )
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(TradePlan).where(TradePlan.analysis_run_id == run_id)
        )
        if existing:
            raise AppError(
                409,
                "ANALYSIS_ALREADY_CONFIRMED",
                "该分析已经保存为正式计划。",
            ) from exc
        raise AppError(
            409,
            "PLAN_VERSION_CONFLICT",
            "计划版本发生并发冲突，请重新分析后确认。",
        ) from exc
    frozen_plan = db.get(TradePlan, saved["id"])
    if frozen_plan:
        frozen_plan.engine_snapshot = {
            **plan,
            "decision_package": decision_package,
            "freeze_hash": decision_package["package_hash"],
        }
        frozen_plan.market_snapshot = {
            "market_assessment": plan.get("market_assessment"),
            "industry_assessment": plan.get("industry_assessment"),
            "quality_snapshot": decision_package["quality_snapshot"],
        }
        frozen_plan.source_snapshot = (
            (result.get("ai") or {}).get("sources")
            or decision_package.get("evidence")
            or plan.get("sources")
            or []
        )
    run.user_confirmed = True
    run.confirmed_plan_id = saved["id"]
    run.status = "confirmed"
    db.commit()
    return {**saved, "run_id": run.id, "user_confirmed": True}


def get_analysis_run(db: Session, run_id: int) -> dict:
    run = db.get(PlanAnalysisRun, run_id)
    if run is None:
        raise AppError(404, "ANALYSIS_RUN_NOT_FOUND", "一键分析记录不存在")
    return {
        "id": run.id,
        "symbol": run.symbol,
        "account_id": run.account_id,
        "position_mode": run.position_mode,
        "status": run.status,
        "steps": run.pipeline_steps,
        "result": run.result_snapshot,
        "ai_analysis_id": run.ai_analysis_id,
        "user_confirmed": run.user_confirmed,
        "confirmed_plan_id": run.confirmed_plan_id,
        "error": run.error,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }
