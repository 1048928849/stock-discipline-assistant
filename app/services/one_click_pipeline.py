from __future__ import annotations

import logging
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data_hub.contracts import ProviderUnavailableError, Quote
from app.data_hub.market_subjects import (
    index_daily_subject,
    sector_daily_subject,
    stock_daily_subject,
)
from app.data_hub.router import DataHubRouter
from app.domain.package_builder import build_decision_package
from app.errors import AppError
from app.models import (
    Account,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchEvidence,
    CompanyValuationSnapshot,
    DataQualityRecord,
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
from app.services.market_cache import (
    effective_quality_metadata,
    market_quality_binding_from_selection,
    mapping_series_bars,
    persist_market_quote,
    replace_market_series,
    resolve_cached_quote,
    resolve_cached_series,
    validate_series_for_persistence,
)
from app.services.research_cache import (
    persist_company_profile,
    resolve_cached_announcement_catalog,
    resolve_cached_company_profile,
)
from app.services.plan_freeze import freeze_trade_plan
from app.services.trade_plan_generator import (
    GENERATOR_PARAMETERS,
    generate_trade_plan_preview,
)


logger = logging.getLogger(__name__)


def _step(code: str, name: str, status: str, detail: str, **extra) -> dict:
    result = {
        "code": code,
        "name": name,
        "status": status,
        "detail": detail,
        "fallback_used": bool(extra.pop("fallback_used", False)),
        "missing": extra.pop("missing", []),
        **extra,
    }
    if code in {
        "market_data",
        "market_quote",
        "market_judgement",
        "industry_judgement",
    }:
        result.setdefault("market_quality_binding", None)
    return result


def _market_binding_payload(selection, data_capability: str) -> dict | None:
    binding = market_quality_binding_from_selection(
        selection,
        data_capability=data_capability,
    )
    return binding.model_dump(mode="json") if binding else None


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


def _sync_stock(
    db: Session, symbol: str, provider: DataHubRouter, refresh: bool
) -> tuple[dict, dict]:
    subject = stock_daily_subject(symbol, "qfq", "CNY", "share")
    cached = resolve_cached_series(
        db,
        cache_symbol=subject.subject_id,
        capability="market.daily.qfq",
        subject=subject,
        adjustment="qfq",
        price_unit="CNY",
        volume_unit="share",
        min_rows=250,
    )
    if cached.executable and not refresh:
        latest = cached.bars[-1]
        quote_step = _cached_quote_step(db, symbol)
        return (
            _step(
                "market_data",
                "行情数据",
                "success",
                f"本地前复权日线有效，截止 {latest.trade_date.isoformat()}。",
                source=cached.source,
                data_time=latest.fetched_at.isoformat(),
                observed_at=latest.observed_at.isoformat(),
                quality_status=cached.effective_quality.effective_quality.value,
                quality_record_id=cached.quality_record_id,
                market_quality_binding=_market_binding_payload(
                    cached, "market.daily.qfq"
                ),
                **effective_quality_metadata(cached.effective_quality),
            ),
            {
                "data_date": latest.trade_date.isoformat(),
                "source": cached.source,
                "quote_step": quote_step,
            },
        )
    try:
        history_result = provider.get_history(
            symbol, date.today() - timedelta(days=900), date.today()
        )
        # Keep acquisition audit durable while cache replacement remains rollbackable.
        db.commit()
        bars = history_result.require_trusted_value()
        validate_series_for_persistence(bars, subject=subject, min_rows=250)
        quote_error = None
        quote_result = None
        try:
            quote_result = provider.get_quote(symbol)
            db.commit()
            quote = quote_result.require_trusted_value()
        except ProviderUnavailableError as exc:
            if quote_result is not None:
                db.commit()
            quote_error = str(exc)
            latest = bars[-1]
            old_quote = db.scalar(select(MarketQuote).where(MarketQuote.symbol == symbol))
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
        replace_market_series(
            db,
            provider,
            history_result,
            bars,
            subject=subject,
            min_rows=250,
        )
        stored_history = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.daily.qfq",
            subject=subject,
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            min_rows=250,
        )
        if not stored_history.executable:
            raise ProviderUnavailableError(
                "stock history refresh blocked by effective quality: "
                f"{stored_history.effective_quality.effective_quality.value} "
                f"({stored_history.blocking_reason or 'not_executable'})"
            )
        db.commit()

        if quote_result is not None:
            try:
                persist_market_quote(db, provider, quote_result)
                candidate_quote = resolve_cached_quote(
                    db,
                    symbol=symbol,
                    capability="market.quote.realtime",
                )
                if not candidate_quote.executable:
                    raise ProviderUnavailableError(
                        "quote refresh blocked by effective quality: "
                        f"{candidate_quote.effective_quality.effective_quality.value} "
                        f"({candidate_quote.blocking_reason or 'not_executable'})"
                    )
                db.commit()
            except Exception as exc:
                db.rollback()
                quote_error = str(exc)
                latest = stored_history.bars[-1]
                quote = Quote(
                    symbol=symbol,
                    name=quote.name,
                    price=latest.close,
                    quote_type="latest_close",
                    observed_at=latest.observed_at,
                    price_unit=latest.price_unit,
                    source=latest.source,
                    source_api="history_latest_close",
                    fetched_at=latest.fetched_at,
                )
        stored_quote = resolve_cached_quote(
            db,
            symbol=symbol,
            capability="market.quote.realtime",
        )
        execution_quote = (
            stored_quote.value if stored_quote.executable else None
        )
        latest = stored_history.bars[-1]
        display_quote = execution_quote or Quote(
            symbol=symbol,
            name=execution_quote.name if execution_quote else symbol,
            price=latest.close,
            quote_type="latest_close",
            observed_at=latest.observed_at,
            price_unit=latest.price_unit,
            source=latest.source,
            source_api="history_latest_close",
            fetched_at=latest.fetched_at,
        )
        used_cached_realtime = execution_quote is not None and quote_error is not None
        quote_step = _step(
            "market_quote",
            "当前价格质量",
            "success"
            if stored_quote.executable
            else "partial",
            (
                "本次刷新失败，继续使用仍新鲜的可信实时缓存。"
                if used_cached_realtime
                else "已取得实时价格。"
                if execution_quote
                else "实时价格不可用；最新收盘价仅用于展示。"
            ),
            source=execution_quote.source if execution_quote else "none",
            observed_at=(
                execution_quote.observed_at.isoformat()
                if execution_quote
                else None
            ),
            fetched_at=(
                execution_quote.fetched_at.isoformat()
                if execution_quote
                else None
            ),
            data_time=(
                execution_quote.observed_at.isoformat()
                if execution_quote
                else None
            ),
            quality_status=(
                stored_quote.effective_quality.effective_quality.value
            ),
            provider_observations=(
                quote_result.provider_observations
                if quote_result is not None
                else []
            ),
            conflict_fields=(
                quote_result.conflict_fields if quote_result is not None else []
            ),
            quote_type=display_quote.quote_type,
            price=str(display_quote.price),
            execution_quote_type=(
                execution_quote.quote_type if execution_quote else None
            ),
            execution_price=(
                str(execution_quote.price) if execution_quote else None
            ),
            display_quote_type=display_quote.quote_type,
            display_price=str(display_quote.price),
            fallback_used=quote_error is not None,
            quality_record_id=stored_quote.quality_record_id,
            market_quality_binding=_market_binding_payload(
                stored_quote, "market.quote.realtime"
            ),
            **effective_quality_metadata(stored_quote.effective_quality),
        )
        return (
            _step(
                "market_data",
                "行情数据",
                "success",
                f"已自动同步 {len(bars)} 根前复权日线，截止 {bars[-1].trade_date.isoformat()}。"
                + (f" 实时行情失败，使用最新收盘：{quote_error}" if quote_error else ""),
                fallback_used=bool(quote_error) or history_result.fallback_used,
                quality_status=stored_history.effective_quality.effective_quality.value,
                observed_at=stored_history.observed_at.isoformat()
                if stored_history.observed_at
                else None,
                fetched_at=stored_history.bars[-1].fetched_at.isoformat(),
                provider_observations=history_result.provider_observations,
                source=stored_history.source,
                data_time=stored_history.bars[-1].fetched_at.isoformat(),
                quality_record_id=stored_history.quality_record_id,
                market_quality_binding=_market_binding_payload(
                    stored_history, "market.daily.qfq"
                ),
                **effective_quality_metadata(stored_history.effective_quality),
            ),
            {
                "data_date": stored_history.bars[-1].trade_date.isoformat(),
                "source": stored_history.source,
                "quote_step": quote_step,
            },
        )
    except Exception as exc:
        db.rollback()
        cached = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.daily.qfq",
            subject=subject,
            adjustment="qfq",
            price_unit="CNY",
            volume_unit="share",
            min_rows=250,
        )
        if cached.executable:
            latest = cached.bars[-1]
            quote_step = _cached_quote_step(db, symbol)
            return (
                _step(
                    "market_data",
                    "行情数据",
                    "partial",
                    f"外部行情同步失败，回退到 {latest.trade_date.isoformat()} 的缓存：{exc}",
                    fallback_used=True,
                    source=cached.source,
                    data_time=latest.fetched_at.isoformat(),
                    observed_at=cached.observed_at.isoformat()
                    if cached.observed_at
                    else None,
                    quality_status=cached.effective_quality.effective_quality.value,
                    quality_record_id=cached.quality_record_id,
                    market_quality_binding=_market_binding_payload(
                        cached, "market.daily.qfq"
                    ),
                    **effective_quality_metadata(cached.effective_quality),
                    missing=["最新行情"],
                ),
                {
                    "data_date": latest.trade_date.isoformat(),
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
                quality_status=cached.effective_quality.effective_quality.value,
                quality_record_id=cached.quality_record_id,
                market_quality_binding=_market_binding_payload(
                    cached, "market.daily.qfq"
                ),
                **effective_quality_metadata(cached.effective_quality),
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
    subject = index_daily_subject("csi000300", "unadjusted", "CNY", "share")
    cached = resolve_cached_series(
        db,
        cache_symbol=subject.subject_id,
        capability="market.index_daily",
        subject=subject,
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        min_rows=60,
    )
    if cached.executable:
        rows = [
            {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
            for item in cached.bars
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
            source=cached.source,
            data_time=cached.bars[-1].fetched_at.isoformat(),
            observed_at=cached.bars[-1].observed_at.isoformat(),
            quality_status=cached.effective_quality.effective_quality.value,
            quality_record_id=cached.quality_record_id,
            market_quality_binding=_market_binding_payload(
                cached, "market.index_daily"
            ),
            **effective_quality_metadata(cached.effective_quality),
        )
    try:
        history_result = provider.get_index_history(
            "csi000300", date.today() - timedelta(days=240), date.today()
        )
        # Preserve the acquisition audit before starting the replace transaction.
        db.commit()
        history = history_result.require_value()
        bars = mapping_series_bars(
            history,
            cache_symbol=subject.subject_id,
            adjustment="unadjusted",
        )
        replace_market_series(
            db,
            provider,
            history_result,
            bars,
            subject=subject,
            min_rows=60,
        )
        current = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.index_daily",
            subject=subject,
            adjustment="unadjusted",
            price_unit="CNY",
            volume_unit="share",
            min_rows=60,
        )
        if not current.executable:
            raise ProviderUnavailableError(
                "index refresh blocked by effective quality: "
                f"{current.effective_quality.effective_quality.value} "
                f"({current.blocking_reason or 'not_executable'})"
            )
        db.commit()
        rows = [
            {
                "date": item.trade_date,
                "close": float(item.close),
                "volume": float(item.volume),
            }
            for item in current.bars
        ]
        assessment = _series_assessment(rows)
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
            source=current.source,
            data_time=current.bars[-1].fetched_at.isoformat(),
            quality_status=current.effective_quality.effective_quality.value,
            observed_at=current.observed_at.isoformat()
            if current.observed_at
            else None,
            fetched_at=current.bars[-1].fetched_at.isoformat(),
            provider_observations=history_result.provider_observations,
            quality_record_id=current.quality_record_id,
            market_quality_binding=_market_binding_payload(
                current, "market.index_daily"
            ),
            **effective_quality_metadata(current.effective_quality),
        )
    except Exception as exc:
        db.rollback()
        cached = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.index_daily",
            subject=subject,
            adjustment="unadjusted",
            price_unit="CNY",
            volume_unit="share",
            min_rows=60,
        )
        if cached.executable:
            rows = [
                {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
                for item in cached.bars
            ]
            assessment = _series_assessment(rows)
            return assessment, _step(
                "market_judgement",
                "市场判断",
                "partial",
                f"指数刷新失败，回退到 {cached.bars[-1].trade_date.isoformat()} 的缓存：{exc}",
                fallback_used=True,
                source=cached.source,
                data_time=cached.bars[-1].fetched_at.isoformat(),
                observed_at=cached.observed_at.isoformat()
                if cached.observed_at
                else None,
                quality_status=cached.effective_quality.effective_quality.value,
                quality_record_id=cached.quality_record_id,
                market_quality_binding=_market_binding_payload(
                    cached, "market.index_daily"
                ),
                **effective_quality_metadata(cached.effective_quality),
                missing=["最新沪深300行情"],
            )
        assessment = {"state": "无法判断", "risk": "无法判断", "return_20d": None}
        return assessment, _step(
            "market_judgement",
            "市场判断",
            "partial",
            f"宽基指数获取失败，市场环境不作猜测：{exc}",
            missing=["沪深300近60个交易日"],
            quality_status=cached.effective_quality.effective_quality.value,
            quality_record_id=cached.quality_record_id,
            market_quality_binding=_market_binding_payload(
                cached, "market.index_daily"
            ),
            **effective_quality_metadata(cached.effective_quality),
        )


def _pick(raw: dict, *keys):
    return next((raw.get(key) for key in keys if raw.get(key) not in (None, "")), None)


def _source_binding_payload(selection, data_capability: str) -> dict | None:
    if (
        not selection.executable
        or selection.quality_record_id is None
        or selection.effective_quality.observed_at is None
    ):
        return None
    payload = {
        "data_capability": data_capability,
        "subject_type": selection.subject.subject_type,
        "subject_id": selection.subject.subject_id,
        "semantic_key": selection.subject.semantic_key or "",
        "quality_record_id": selection.quality_record_id,
        "observed_at": selection.effective_quality.observed_at.isoformat(),
    }
    if data_capability == "announcement.catalog":
        payload.update(
            {
                "scan_start": selection.scan_start.isoformat(),
                "scan_end": selection.scan_end.isoformat(),
                "checked_at": selection.checked_at.isoformat(),
            }
        )
    return payload


def _ensure_profile(
    db: Session, symbol: str, provider: DataHubRouter
) -> tuple[CompanyProfile | None, dict]:
    selected = resolve_cached_company_profile(db, symbol)
    profile = selected.profile
    if profile and profile.industry:
        profile_quality = selected.effective_quality.effective_quality.value
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "success",
            f"{profile.name}，所属行业：{profile.industry}。",
            source=profile.source,
            data_time=profile.fetched_at.isoformat(),
            observed_at=profile.fetched_at.isoformat(),
            quality_status=profile_quality,
            source_quality_binding=_source_binding_payload(
                selected, "fundamental.profile"
            ),
            missing=["概念板块自动映射", "完整产业链节点"] if not profile.raw_data else [],
        )
    try:
        profile_result = provider.company_profile(symbol)
        db.commit()
        profile = persist_company_profile(db, provider, profile_result)
        selected = resolve_cached_company_profile(db, symbol)
        if not selected.executable:
            raise ProviderUnavailableError(
                "profile refresh blocked by effective quality: "
                f"{selected.effective_quality.effective_quality.value}"
            )
        db.commit()
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "success" if profile.industry else "partial",
            f"识别为 {profile.name}；行业：{profile.industry or '暂无可靠数据'}。",
            source=profile.source,
            data_time=profile.fetched_at.isoformat(),
            observed_at=profile_result.observed_at.isoformat()
            if profile_result.observed_at
            else None,
            fetched_at=profile_result.fetched_at.isoformat(),
            quality_status=profile_result.quality_status.value,
            source_quality_binding=_source_binding_payload(
                selected, "fundamental.profile"
            ),
            provider_observations=profile_result.provider_observations,
            missing=[] if profile.industry else ["所属行业", "概念板块", "产业链节点"],
        )
    except Exception as exc:
        db.rollback()
        selected = resolve_cached_company_profile(db, symbol)
        profile = selected.profile
        return profile, _step(
            "company_mapping",
            "公司与行业识别",
            "partial",
            f"公司行业识别失败，保留已有信息：{exc}",
            fallback_used=profile is not None,
            observed_at=(
                selected.observed_at.isoformat() if selected.observed_at else None
            ),
            quality_status=selected.effective_quality.effective_quality.value,
            source_quality_binding=_source_binding_payload(
                selected, "fundamental.profile"
            ),
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


def _build_sector_assessment(
    profile: CompanyProfile,
    board_name: str,
    rows: list[dict],
    market: dict,
) -> dict:
    assessment = _series_assessment(rows)
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
    return {
        "state": state,
        "industry": profile.industry,
        "board_name": board_name,
        "return_20d": assessment["return_20d"],
        "relative_20d": round(relative, 2) if relative is not None else None,
        "is_mainline": bool(state == "强"),
        "mainline_method": "行业20日涨跌相对沪深300的规则代理，不等同于完整资金主线模型",
    }


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
    board_name = _sector_board_name(profile)
    subject = sector_daily_subject(board_name, "unadjusted", "CNY", "share")
    cached = resolve_cached_series(
        db,
        cache_symbol=subject.subject_id,
        capability="market.sector_daily",
        subject=subject,
        adjustment="unadjusted",
        price_unit="CNY",
        volume_unit="share",
        min_rows=60,
    )
    if cached.executable and not refresh:
        rows = [
            {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
            for item in cached.bars
        ]
        result = _build_sector_assessment(profile, board_name, rows, market)
        return result, _step(
            "industry_judgement",
            "行业判断",
            "success",
            f"使用有效行业行情缓存，判定为{result['state']}。",
            fallback_used=True,
            source=cached.source,
            data_time=cached.bars[-1].fetched_at.isoformat(),
            observed_at=cached.observed_at.isoformat()
            if cached.observed_at
            else None,
            quality_status=cached.effective_quality.effective_quality.value,
            quality_record_id=cached.quality_record_id,
            market_quality_binding=_market_binding_payload(
                cached, "market.sector_daily"
            ),
            **effective_quality_metadata(cached.effective_quality),
        )
    try:
        history_result = provider.get_sector_history(
            board_name, date.today() - timedelta(days=240), date.today()
        )
        # Preserve the acquisition audit before starting the replace transaction.
        db.commit()
        history = history_result.require_value()
        bars = mapping_series_bars(
            history,
            cache_symbol=subject.subject_id,
            adjustment="unadjusted",
        )
        replace_market_series(
            db,
            provider,
            history_result,
            bars,
            subject=subject,
            min_rows=60,
        )
        current = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.sector_daily",
            subject=subject,
            adjustment="unadjusted",
            price_unit="CNY",
            volume_unit="share",
            min_rows=60,
        )
        if not current.executable:
            raise ProviderUnavailableError(
                "sector refresh blocked by effective quality: "
                f"{current.effective_quality.effective_quality.value} "
                f"({current.blocking_reason or 'not_executable'})"
            )
        db.commit()
        rows = [
            {
                "date": item.trade_date,
                "close": float(item.close),
                "volume": float(item.volume),
            }
            for item in current.bars
        ]
        result = _build_sector_assessment(profile, board_name, rows, market)
        return result, _step(
            "industry_judgement",
            "行业判断",
            "success",
            f"{profile.industry}（行情代理板块：{board_name}）20日涨跌 {result['return_20d']}%，"
            f"相对沪深300 {result['relative_20d']}个百分点，判定为{result['state']}。",
            source=current.source,
            data_time=current.bars[-1].fetched_at.isoformat(),
            quality_status=current.effective_quality.effective_quality.value,
            observed_at=current.observed_at.isoformat()
            if current.observed_at
            else None,
            fetched_at=current.bars[-1].fetched_at.isoformat(),
            provider_observations=history_result.provider_observations,
            quality_record_id=current.quality_record_id,
            market_quality_binding=_market_binding_payload(
                current, "market.sector_daily"
            ),
            **effective_quality_metadata(current.effective_quality),
        )
    except Exception as exc:
        db.rollback()
        cached = resolve_cached_series(
            db,
            cache_symbol=subject.subject_id,
            capability="market.sector_daily",
            subject=subject,
            adjustment="unadjusted",
            price_unit="CNY",
            volume_unit="share",
            min_rows=60,
        )
        if cached.executable:
            rows = [
                {"date": item.trade_date, "close": float(item.close), "volume": float(item.volume)}
                for item in cached.bars
            ]
            result = _build_sector_assessment(profile, board_name, rows, market)
            return result, _step(
                "industry_judgement",
                "行业判断",
                "partial",
                f"行业指数刷新失败，使用有效缓存：{exc}",
                fallback_used=True,
                source=cached.source,
                data_time=cached.bars[-1].fetched_at.isoformat(),
                observed_at=cached.observed_at.isoformat()
                if cached.observed_at
                else None,
                quality_status=cached.effective_quality.effective_quality.value,
                quality_record_id=cached.quality_record_id,
                market_quality_binding=_market_binding_payload(
                    cached, "market.sector_daily"
                ),
                **effective_quality_metadata(cached.effective_quality),
            )
        result = {"state": "无法判断", "relative_20d": None, "is_mainline": None}
        return result, _step(
            "industry_judgement",
            "行业判断",
            "partial",
            f"已识别行业“{profile.industry}”，但行业指数获取失败：{exc}",
            missing=["行业指数近60个交易日", "行业相对强度"],
            quality_status=cached.effective_quality.effective_quality.value,
            quality_record_id=cached.quality_record_id,
            market_quality_binding=_market_binding_payload(
                cached, "market.sector_daily"
            ),
            **effective_quality_metadata(cached.effective_quality),
        )


def _research_inventory(db: Session, symbol: str) -> tuple[dict, dict]:
    announcement_start = date.today() - timedelta(days=3 * 366)
    announcement_end = date.today()
    announcement_selection = resolve_cached_announcement_catalog(
        db, symbol, announcement_start, announcement_end
    )
    financials = db.scalars(
        select(CompanyFinancialPeriod).where(CompanyFinancialPeriod.symbol == symbol)
    ).all()
    announcements = announcement_selection.announcements
    evidence = db.scalars(
        select(CompanyResearchEvidence).where(CompanyResearchEvidence.symbol == symbol)
    ).all()
    valuation = db.scalar(
        select(CompanyValuationSnapshot)
        .where(CompanyValuationSnapshot.symbol == symbol)
        .order_by(CompanyValuationSnapshot.trade_date.desc())
    )
    announcement_scan = announcement_selection.refresh
    risks = [item for item in announcements if item.risk_level in {"红", "黄"}]
    missing = []
    if not financials:
        missing.append("最近12季度财务数据")
    if not announcement_selection.executable:
        missing.append("公司公告目录")
    if not evidence:
        missing.append("产业与公开信息证据")
    if not valuation:
        missing.append("估值数据")
    result = {
        "financial_periods": len(financials),
        "announcements": len(announcements),
        "announcement_scan_completed": announcement_selection.executable,
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
    announcement_detail = (
        "公告目录扫描成功，当前覆盖范围未发现公告。"
        if announcement_selection.executable and not announcements
        else f"本地已有财务期数 {len(financials)}、公告 {len(announcements)}、产业证据 {len(evidence)}。"
    )
    return result, _step(
        "company_risk",
        "公司风险与公开信息",
        "success" if not missing else "partial",
        announcement_detail,
        missing=missing,
        source="本地公司研究中心（原始来源保留在证据记录）",
        data_time=datetime.now().isoformat(),
        observed_at=announcement_scan.checked_at.isoformat()
        if announcement_scan and announcement_scan.checked_at
        else None,
        quality_status=(
            announcement_selection.effective_quality.effective_quality.value
        ),
        source_quality_binding=_source_binding_payload(
            announcement_selection, "announcement.catalog"
        ),
        required_missing=(
            [] if announcement_selection.executable else ["announcements"]
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
    selection = resolve_cached_quote(
        db,
        symbol=symbol,
        capability="market.quote.realtime",
    )
    execution_quote = selection.value if selection.executable else None
    display_quote = execution_quote
    if display_quote is None:
        display_selection = resolve_cached_quote(
            db,
            symbol=symbol,
            capability="market.quote.latest_close",
        )
        display_quote = display_selection.value
    quote = execution_quote
    quality = selection.effective_quality.effective_quality.value
    record_id = (
        selection.effective_quality.blocking_record_id
        or selection.quality_record_id
    )
    record = db.get(DataQualityRecord, record_id) if record_id else None
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
        quality_record_id=selection.quality_record_id,
        market_quality_binding=_market_binding_payload(
            selection, "market.quote.realtime"
        ),
        **effective_quality_metadata(selection.effective_quality),
        quote_type=display_quote.quote_type if display_quote else None,
        price=str(display_quote.price) if display_quote else None,
        execution_quote_type=quote.quote_type if quote else None,
        execution_price=str(quote.price) if quote else None,
        display_quote_type=display_quote.quote_type if display_quote else None,
        display_price=str(display_quote.price) if display_quote else None,
        fallback_used=display_quote is not None and quote is None,
    )


def _decision(preview: dict, position_mode: str) -> dict:
    holding = preview["existing_position"]
    if position_mode == "持仓":
        if preview.get("price_trigger_evaluation_skipped"):
            status, label = "WAIT", "等待可信实时价格"
        elif holding["hard_stop_triggered"] or preview.get("pattern", {}).get("platform_broken"):
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
