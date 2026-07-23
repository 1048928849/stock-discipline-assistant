from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AIAnalysis,
    CompanyAnnouncement,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchEvidence,
    CompanyValuationSnapshot,
    XPost,
)
from app.providers.akshare_provider import AKShareProvider


PROFILE_URL = "http://www.cninfo.com.cn/new/commonUrl?url=data/stock/stockDetail"
FINANCIAL_URL = (
    "https://money.finance.sina.com.cn/corp/go.php/vFD_FinanceSummary/stockid/{symbol}.phtml"
)
VALUATION_URL = "https://gushitong.baidu.com/stock/ab-{symbol}"


def _decimal(value):
    if value is None or pd.isna(value):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        return None


def _float(value):
    numeric = _decimal(value)
    return float(numeric) if numeric is not None else None


def _ratio(numerator, denominator):
    a, b = _float(numerator), _float(denominator)
    return a / b if a is not None and b not in (None, 0) else None


def _change(current, previous):
    a, b = _float(current), _float(previous)
    return (a / b - 1) * 100 if a is not None and b not in (None, 0) else None


def _date(value) -> date:
    return pd.Timestamp(value).date()


def _period_label(report_date: date) -> str:
    return {3: "一季报", 6: "半年报", 9: "三季报", 12: "年报"}.get(report_date.month, "定期报告")


def _single_quarter(records: dict[date, dict], report_date: date, field: str):
    current = _float(records.get(report_date, {}).get(field))
    if current is None or report_date.month == 3:
        return current
    previous_month = {6: 3, 9: 6, 12: 9}.get(report_date.month)
    previous = _float(records.get(date(report_date.year, previous_month, 30), {}).get(field))
    if previous is None and previous_month == 3:
        previous = _float(records.get(date(report_date.year, 3, 31), {}).get(field))
    return current - previous if previous is not None else None


def _statement_maps(statements: dict[str, list[dict]]) -> dict[str, dict[date, dict]]:
    result = {}
    for name, rows in statements.items():
        mapped = {}
        for row in rows:
            if row.get("报告日"):
                mapped[_date(str(row["报告日"]))] = row
        result[name] = mapped
    return result


def _financial_values(statements: dict[str, list[dict]]) -> list[dict]:
    maps = _statement_maps(statements)
    dates = sorted(set(maps["balance"]) & set(maps["income"]) & set(maps["cash_flow"]))[-12:]
    values = []
    for report_date in dates:
        balance = maps["balance"][report_date]
        income = maps["income"][report_date]
        cash = maps["cash_flow"][report_date]
        revenue_sq = _single_quarter(maps["income"], report_date, "营业收入")
        cost_sq = _single_quarter(maps["income"], report_date, "营业成本")
        profit_field = (
            "归属于母公司所有者的净利润"
            if income.get("归属于母公司所有者的净利润") is not None
            else "净利润"
        )
        profit_sq = _single_quarter(maps["income"], report_date, profit_field)
        cash_sq = _single_quarter(maps["cash_flow"], report_date, "经营活动产生的现金流量净额")
        equity = balance.get("归属于母公司股东权益合计")
        if equity is None:
            equity = balance.get("所有者权益(或股东权益)合计")
        gross_margin = (
            (revenue_sq - cost_sq) / revenue_sq
            if revenue_sq not in (None, 0) and cost_sq is not None
            else None
        )
        values.append(
            {
                "report_date": report_date,
                "period_label": _period_label(report_date),
                "revenue": _decimal(income.get("营业收入")),
                "operating_cost": _decimal(income.get("营业成本")),
                "net_profit": _decimal(income.get("净利润")),
                "parent_net_profit": _decimal(income.get("归属于母公司所有者的净利润")),
                "operating_cash_flow": _decimal(cash.get("经营活动产生的现金流量净额")),
                "total_assets": _decimal(balance.get("资产总计")),
                "total_liabilities": _decimal(balance.get("负债合计")),
                "equity": _decimal(equity),
                "accounts_receivable": _decimal(balance.get("应收账款")),
                "inventory": _decimal(balance.get("存货")),
                "research_expense": _decimal(income.get("研发费用")),
                "revenue_single_quarter": _decimal(revenue_sq),
                "profit_single_quarter": _decimal(profit_sq),
                "cash_flow_single_quarter": _decimal(cash_sq),
                "gross_margin": _decimal(gross_margin),
                "roe": _decimal(_ratio((profit_sq or 0) * 4, equity)),
                "debt_ratio": _decimal(_ratio(balance.get("负债合计"), balance.get("资产总计"))),
                "raw_data": {"balance": balance, "income": income, "cash_flow": cash},
            }
        )
    return values


ANNOUNCEMENT_RULES = (
    ("监管问询", ("问询函", "监管函", "关注函")),
    ("审计意见", ("审计意见", "保留意见", "无法表示意见", "否定意见")),
    ("诉讼", ("诉讼", "仲裁")),
    ("减持", ("减持",)),
    ("质押", ("质押",)),
    ("担保", ("担保",)),
    ("解禁", ("解除限售", "限售股份上市流通", "解禁")),
    ("业绩预告", ("业绩预告", "业绩快报")),
    ("年报", ("年度报告", "年报")),
)


def classify_announcement(title: str) -> tuple[str, str]:
    category = next(
        (name for name, keywords in ANNOUNCEMENT_RULES if any(key in title for key in keywords)),
        "其他公告",
    )
    if category == "年报" and (
        "半年度报告" in title or any(key in title for key in ("制度", "说明会"))
    ):
        category = "其他公告"
    red_keywords = ("立案调查", "退市风险", "无法表示意见", "否定意见", "重大诉讼")
    yellow_categories = {"监管问询", "诉讼", "减持", "质押", "担保", "解禁", "审计意见"}
    if any(key in title for key in red_keywords):
        risk = "红"
    elif category in yellow_categories or any(key in title for key in ("预亏", "首亏", "续亏")):
        risk = "黄"
    else:
        risk = "灰"
    return category, risk


def _announcement_fields(row: dict) -> tuple[str, date, str, str]:
    title = str(row.get("公告标题") or row.get("标题") or "未命名公告")
    raw_date = row.get("公告时间") or row.get("公告日期") or row.get("日期")
    published = _date(raw_date)
    url = str(row.get("公告链接") or row.get("网址") or row.get("链接") or "")
    catalog = str(row.get("目录来源") or "巨潮资讯")
    return title, published, url, catalog


def _resolve_announcement_document_url(url: str) -> str | None:
    """Resolve an Eastmoney catalogue link to the exchange-file PDF when possible."""
    if url.lower().endswith(".pdf"):
        return url
    match = re.search(r"(AN\d+)", url)
    if not match:
        return None
    try:
        import httpx

        response = httpx.get(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            params={"art_code": match.group(1), "client_source": "web", "page_index": 1},
            timeout=8,
        )
        response.raise_for_status()
        return response.json().get("data", {}).get("attach_url") or None
    except Exception:
        return None


def _percentile(values: list[float], current: float | None):
    clean = [value for value in values if value is not None and value > 0]
    if current is None or not clean:
        return None
    return sum(value <= current for value in clean) / len(clean)


def _latest_history(rows: list[dict]):
    valid = [(item.get("date"), _float(item.get("value"))) for item in rows]
    valid = [(d, value) for d, value in valid if d is not None and value is not None]
    return sorted(valid, key=lambda item: str(item[0]))[-1] if valid else (None, None)


def _ps_history(financials: list[CompanyFinancialPeriod], market_cap_rows: list[dict]):
    cap = pd.Series(
        {_date(row["date"]): _float(row.get("value")) for row in market_cap_rows if row.get("date")}
    ).sort_index()
    periods = sorted(financials, key=lambda item: item.report_date)
    values = []
    for index in range(3, len(periods)):
        revenue = sum(
            (_float(item.revenue_single_quarter) or 0) for item in periods[index - 3 : index + 1]
        )
        if revenue <= 0 or cap.empty:
            continue
        available = cap.loc[cap.index <= periods[index].report_date]
        if not available.empty:
            values.append(float(available.iloc[-1]) / (revenue / 1e8))
    return values


def _implied_scenarios(current_pe: float | None, pe_history: list[float]):
    clean = pd.Series([value for value in pe_history if value and value > 0])
    if current_pe is None or clean.empty:
        return None
    targets = {
        "乐观": float(clean.quantile(0.75)),
        "中性": float(clean.quantile(0.5)),
        "悲观": float(clean.quantile(0.25)),
    }
    return {
        name: {
            "three_year_target_pe": round(target, 2),
            "implied_annual_profit_growth_pct": round(
                ((current_pe / target) ** (1 / 3) - 1) * 100, 2
            ),
            "explanation": "假设三年后估值回到该历史位置，反推当前市值需要的年均利润增长。",
        }
        for name, target in targets.items()
        if target > 0
    }


def _peer_operating_comparison(provider: AKShareProvider, peer_rows: list[dict]) -> list[dict]:
    result = []
    for peer in peer_rows[:3]:
        code = str(peer.get("代码", ""))
        try:
            periods = _financial_values(provider.financial_statements(code))
            latest = periods[-1]
            prior = periods[-5] if len(periods) >= 5 else None
            result.append(
                {
                    "symbol": code,
                    "name": peer.get("简称"),
                    "report_date": latest["report_date"].isoformat(),
                    "revenue_yoy_pct": round(
                        _change(latest["revenue_single_quarter"], prior["revenue_single_quarter"]),
                        2,
                    )
                    if prior
                    and _change(latest["revenue_single_quarter"], prior["revenue_single_quarter"])
                    is not None
                    else None,
                    "profit_yoy_pct": round(
                        _change(latest["profit_single_quarter"], prior["profit_single_quarter"]),
                        2,
                    )
                    if prior
                    and _change(latest["profit_single_quarter"], prior["profit_single_quarter"])
                    is not None
                    else None,
                    "gross_margin_pct": round((_float(latest["gross_margin"]) or 0) * 100, 2),
                    "roe_annualized_pct": round((_float(latest["roe"]) or 0) * 100, 2),
                    "source": "新浪财经三大财务报表",
                }
            )
        except Exception:
            continue
    return result


def _evidence_key(symbol: str, topic: str, content: str, url: str | None) -> str:
    return hashlib.sha256(f"{symbol}|{topic}|{content}|{url or ''}".encode()).hexdigest()


def _upsert_evidence(db: Session, **values) -> None:
    key = _evidence_key(
        values["symbol"], values["topic"], values["content"], values.get("source_url")
    )
    item = db.scalar(
        select(CompanyResearchEvidence).where(CompanyResearchEvidence.evidence_key == key)
    )
    if item is None:
        db.add(CompanyResearchEvidence(evidence_key=key, **values))


TOPIC_KEYWORDS = {
    "主营业务": ("主营业务", "主要业务"),
    "上下游": ("上游", "下游", "供应商", "采购模式", "销售模式"),
    "主要客户": ("主要客户", "前五名客户", "客户集中度"),
    "产能": ("产能", "产量", "在建项目", "募投项目"),
    "研发": ("研发投入", "研发人员", "研发费用"),
    "竞争优势": ("核心竞争力", "竞争优势"),
}


def extract_report_evidence(text: str) -> list[dict]:
    normalized = re.sub(r"\s+", " ", text)
    evidence = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        match = next(
            (re.search(re.escape(key), normalized) for key in keywords if key in normalized), None
        )
        if match:
            start, end = max(0, match.start() - 100), min(len(normalized), match.start() + 320)
            evidence.append({"topic": topic, "content": normalized[start:end].strip()})
    return evidence


def _annual_report_text(announcement: CompanyAnnouncement) -> str | None:
    match = re.search(r"(AN\d+)", announcement.url)
    if not match:
        return None
    try:
        import httpx
        from pypdf import PdfReader

        response = httpx.get(
            "https://np-cnotice-stock.eastmoney.com/api/content/ann",
            params={"art_code": match.group(1), "client_source": "web", "page_index": 1},
            timeout=20,
        )
        response.raise_for_status()
        pdf_url = announcement.source_document_url or response.json().get("data", {}).get(
            "attach_url"
        )
        if not pdf_url:
            return response.json().get("data", {}).get("notice_content")
        pdf = httpx.get(pdf_url, timeout=30)
        pdf.raise_for_status()
        if len(pdf.content) > 30 * 1024 * 1024:
            return None
        reader = PdfReader(io.BytesIO(pdf.content))
        pages = []
        for page in reader.pages[:250]:
            page_text = page.extract_text() or ""
            if any(
                keyword in page_text for keywords in TOPIC_KEYWORDS.values() for keyword in keywords
            ):
                pages.append(page_text)
        return "\n".join(pages)
    except Exception:
        return None


def sync_company_research(
    db: Session,
    symbol: str,
    provider: AKShareProvider | None = None,
    include_documents: bool = True,
) -> dict:
    provider = provider or AKShareProvider(retries=1)
    fetched_at = datetime.now()
    sections = {}
    profile = None

    try:
        raw_profile = provider.company_profile(symbol)
        profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
        values = {
            "name": str(raw_profile.get("A股简称") or raw_profile.get("公司名称") or symbol),
            "industry": raw_profile.get("细分行业") or raw_profile.get("所属行业"),
            "market": raw_profile.get("所属市场"),
            "main_business": raw_profile.get("主营业务"),
            "business_scope": raw_profile.get("经营范围"),
            "website": raw_profile.get("官方网站"),
            "source": "巨潮资讯公司概况",
            "source_url": PROFILE_URL,
            "raw_data": raw_profile,
            "fetched_at": fetched_at,
        }
        if profile is None:
            profile = CompanyProfile(symbol=symbol, **values)
            db.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
        if profile.main_business:
            _upsert_evidence(
                db,
                symbol=symbol,
                topic="主营业务",
                information_type="事实",
                content=profile.main_business,
                source_name="巨潮资讯公司概况",
                source_url=PROFILE_URL,
                source_date=None,
                raw_data={"field": "主营业务"},
                fetched_at=fetched_at,
            )
        sections["profile"] = {"status": "success", "rows": 1}
    except Exception as exc:
        sections["profile"] = {"status": "unavailable", "message": str(exc)}

    try:
        statements = provider.financial_statements(symbol)
        rows = _financial_values(statements)
        for values in rows:
            item = db.scalar(
                select(CompanyFinancialPeriod).where(
                    CompanyFinancialPeriod.symbol == symbol,
                    CompanyFinancialPeriod.report_date == values["report_date"],
                )
            )
            payload = {
                **values,
                "source": "新浪财经三大财务报表",
                "source_url": FINANCIAL_URL.format(symbol=symbol),
                "fetched_at": fetched_at,
            }
            if item is None:
                db.add(CompanyFinancialPeriod(symbol=symbol, **payload))
            else:
                for key, value in payload.items():
                    setattr(item, key, value)
        sections["financials"] = {"status": "success", "rows": len(rows)}
        db.flush()
    except Exception as exc:
        sections["financials"] = {"status": "unavailable", "message": str(exc)}

    try:
        announcement_rows = provider.company_announcements(
            symbol, date.today() - timedelta(days=3 * 366), date.today()
        )
        inserted = 0
        document_resolution_attempts = 0
        resolved_documents = 0
        for row in announcement_rows:
            title, published, url, catalog = _announcement_fields(row)
            if not url:
                continue
            category, risk = classify_announcement(title)
            document_url = None
            should_resolve = risk in {"红", "黄"} or category == "年报"
            if should_resolve and document_resolution_attempts < 8:
                document_resolution_attempts += 1
                document_url = _resolve_announcement_document_url(url)
                if document_url:
                    resolved_documents += 1
            item = db.scalar(
                select(CompanyAnnouncement).where(
                    CompanyAnnouncement.symbol == symbol, CompanyAnnouncement.url == url
                )
            )
            values = {
                "title": title,
                "announcement_category": category,
                "risk_level": risk,
                "published_date": published,
                "catalog_source": catalog,
                "exchange": "上交所" if symbol.startswith(("5", "6", "9")) else "深交所",
                "url": url,
                "source_document_url": document_url,
                "raw_data": row,
                "fetched_at": fetched_at,
            }
            if item is None:
                db.add(CompanyAnnouncement(symbol=symbol, **values))
                inserted += 1
            else:
                for key, value in values.items():
                    setattr(item, key, value)
        sections["announcements"] = {
            "status": "success",
            "rows": len(announcement_rows),
            "inserted": inserted,
            "original_documents_resolved": resolved_documents,
        }
        db.flush()
    except Exception as exc:
        sections["announcements"] = {"status": "unavailable", "message": str(exc)}

    try:
        history = provider.valuation_history(symbol)
        comparison = []
        try:
            comparison = provider.valuation_comparison(symbol)
        except Exception:
            comparison = []
        pe_date, pe = _latest_history(history.get("pe_ttm", []))
        pb_date, pb = _latest_history(history.get("pb", []))
        cap_date, cap = _latest_history(history.get("market_cap", []))
        target_row = next((row for row in comparison if str(row.get("代码")) == symbol), {})
        ps = _float(target_row.get("市销率-TTM"))
        financials = db.scalars(
            select(CompanyFinancialPeriod)
            .where(CompanyFinancialPeriod.symbol == symbol)
            .order_by(CompanyFinancialPeriod.report_date)
        ).all()
        ps_values = _ps_history(financials, history.get("market_cap", []))
        pe_values = [_float(row.get("value")) for row in history.get("pe_ttm", [])]
        pb_values = [_float(row.get("value")) for row in history.get("pb", [])]
        trade_date = _date(
            max(value for value in (pe_date, pb_date, cap_date) if value is not None)
        )
        item = db.scalar(
            select(CompanyValuationSnapshot).where(
                CompanyValuationSnapshot.symbol == symbol,
                CompanyValuationSnapshot.trade_date == trade_date,
            )
        )
        industry_median = next(
            (row for row in comparison if str(row.get("代码")) == "行业中值"), None
        )
        peer_rows = [
            row
            for row in comparison
            if str(row.get("代码", "")).isdigit() and str(row.get("代码")) != symbol
        ][:5]
        peer_operating = _peer_operating_comparison(provider, peer_rows)
        values = {
            "market_cap": _decimal(cap),
            "pe_ttm": _decimal(pe),
            "pb": _decimal(pb),
            "ps_ttm": _decimal(ps),
            "pe_percentile": _decimal(_percentile(pe_values, pe)),
            "pb_percentile": _decimal(_percentile(pb_values, pb)),
            "ps_percentile": _decimal(_percentile(ps_values, ps)),
            "industry_comparison": {
                "industry_median": industry_median,
                "peers": peer_rows,
                "peer_operating": peer_operating,
                "operating_note": (
                    "经营指标按各公司最近可得报告期计算，报告期可能不完全一致。"
                    if peer_operating
                    else "暂无可靠同行经营数据"
                ),
            },
            "implied_growth": _implied_scenarios(pe, pe_values),
            "source": "百度股市通历史估值 + 东方财富同行比较",
            "source_url": VALUATION_URL.format(symbol=symbol),
            "fetched_at": fetched_at,
        }
        if item is None:
            db.add(CompanyValuationSnapshot(symbol=symbol, trade_date=trade_date, **values))
        else:
            for key, value in values.items():
                setattr(item, key, value)
        sections["valuation"] = {"status": "success", "rows": 1}
    except Exception as exc:
        sections["valuation"] = {"status": "unavailable", "message": str(exc)}

    if include_documents:
        annual_report = db.scalar(
            select(CompanyAnnouncement)
            .where(
                CompanyAnnouncement.symbol == symbol,
                CompanyAnnouncement.announcement_category == "年报",
                ~CompanyAnnouncement.title.contains("摘要"),
            )
            .order_by(CompanyAnnouncement.published_date.desc())
        )
        text = _annual_report_text(annual_report) if annual_report else None
        extracted = extract_report_evidence(text) if text else []
        for item in extracted:
            _upsert_evidence(
                db,
                symbol=symbol,
                topic=item["topic"],
                information_type="公司表态",
                content=item["content"],
                source_name=annual_report.title,
                source_url=annual_report.source_document_url or annual_report.url,
                source_date=annual_report.published_date,
                raw_data={"document_type": "annual_report"},
                fetched_at=fetched_at,
            )
        sections["annual_report_parse"] = {
            "status": "success" if extracted else "unavailable",
            "rows": len(extracted),
            "message": None if extracted else "年报原文中暂无可可靠提取的对应段落",
        }

    company_name = profile.name if profile else ""
    x_posts = db.scalars(select(XPost).order_by(XPost.published_at.desc()).limit(500)).all()
    matched_posts = [
        post
        for post in x_posts
        if symbol in post.content or (company_name and company_name in post.content)
    ]
    for post in matched_posts:
        analysis = db.scalar(
            select(AIAnalysis).where(
                AIAnalysis.content_type == "x_post",
                AIAnalysis.content_id == post.id,
                AIAnalysis.status == "success",
            )
        )
        information_type = (
            analysis.result.get("information_type", "个人观点")
            if analysis and analysis.result
            else "个人观点"
        )
        _upsert_evidence(
            db,
            symbol=symbol,
            topic="产业资讯",
            information_type=information_type,
            content=post.content[:2000],
            source_name=f"X / @{post.author}",
            source_url=post.url,
            source_date=post.published_at.date(),
            raw_data={"post_id": post.post_id},
            fetched_at=fetched_at,
        )

    db.commit()
    return {
        "symbol": symbol,
        "status": "partial"
        if any(v["status"] != "success" for v in sections.values())
        else "success",
        "sections": sections,
        "updated_at": fetched_at.isoformat(),
    }


def _metric_text(value, suffix="%"):
    return "暂无可靠数据" if value is None else f"{value:.2f}{suffix}"


def _financial_report(rows: list[CompanyFinancialPeriod]) -> dict:
    if not rows:
        return {
            "status": "unavailable",
            "message": "暂无可靠数据",
            "conclusions": ["尚未取得最近季度的三大财务报表。"],
            "trends": [],
            "anomalies": [],
            "chart": [],
        }
    rows = sorted(rows, key=lambda item: item.report_date)[-12:]
    latest = rows[-1]
    prior = rows[-5] if len(rows) >= 5 else None
    revenue_yoy = _change(
        latest.revenue_single_quarter, prior.revenue_single_quarter if prior else None
    )
    profit_yoy = _change(
        latest.profit_single_quarter, prior.profit_single_quarter if prior else None
    )
    cash_yoy = _change(
        latest.cash_flow_single_quarter, prior.cash_flow_single_quarter if prior else None
    )
    receivable_yoy = _change(
        latest.accounts_receivable, prior.accounts_receivable if prior else None
    )
    inventory_yoy = _change(latest.inventory, prior.inventory if prior else None)
    gross_margin_delta = (
        (_float(latest.gross_margin) - _float(prior.gross_margin)) * 100
        if prior
        and _float(latest.gross_margin) is not None
        and _float(prior.gross_margin) is not None
        else None
    )
    ocf_profit = _ratio(latest.cash_flow_single_quarter, latest.profit_single_quarter)

    trends = [
        {
            "name": "单季营收同比",
            "value": revenue_yoy,
            "display": _metric_text(revenue_yoy),
            "explanation": "比较本季度与上年同季度营业收入。",
        },
        {
            "name": "单季归母利润同比",
            "value": profit_yoy,
            "display": _metric_text(profit_yoy),
            "explanation": "比较本季度与上年同季度归母净利润。",
        },
        {
            "name": "经营现金流同比",
            "value": cash_yoy,
            "display": _metric_text(cash_yoy),
            "explanation": "比较本季度经营活动现金净流量与上年同季度。",
        },
        {
            "name": "毛利率变化",
            "value": gross_margin_delta,
            "display": _metric_text(gross_margin_delta, "个百分点"),
            "explanation": "按单季度收入和成本计算，正值代表毛利率提升。",
        },
        {
            "name": "年化 ROE",
            "value": (_float(latest.roe) * 100) if latest.roe is not None else None,
            "display": _metric_text((_float(latest.roe) * 100) if latest.roe is not None else None),
            "explanation": "单季利润乘四后除以期末归母权益，仅用于趋势观察。",
        },
        {
            "name": "资产负债率",
            "value": (_float(latest.debt_ratio) * 100) if latest.debt_ratio is not None else None,
            "display": _metric_text(
                (_float(latest.debt_ratio) * 100) if latest.debt_ratio is not None else None
            ),
            "explanation": "总负债占总资产比例。",
        },
        {
            "name": "经营现金流/利润",
            "value": ocf_profit,
            "display": _metric_text(ocf_profit, "倍"),
            "explanation": "用于观察账面利润转化为经营现金的程度。",
        },
    ]
    anomalies = []
    if ocf_profit is not None and ocf_profit < 0.7:
        anomalies.append(
            {
                "level": "黄",
                "title": "现金流弱于利润",
                "explanation": f"经营现金流约为单季利润的 {ocf_profit:.2f} 倍，需核对应收、存货和结算周期。",
            }
        )
    if revenue_yoy is not None and receivable_yoy is not None and receivable_yoy > revenue_yoy + 20:
        anomalies.append(
            {
                "level": "黄",
                "title": "应收增长快于收入",
                "explanation": f"应收同比 {receivable_yoy:.2f}%，高于营收同比 {_metric_text(revenue_yoy)}。",
            }
        )
    if revenue_yoy is not None and inventory_yoy is not None and inventory_yoy > revenue_yoy + 20:
        anomalies.append(
            {
                "level": "黄",
                "title": "存货增长快于收入",
                "explanation": f"存货同比 {inventory_yoy:.2f}%，需结合备货、价格和减值政策核验。",
            }
        )
    if gross_margin_delta is not None and gross_margin_delta < -5:
        anomalies.append(
            {
                "level": "黄",
                "title": "毛利率明显下降",
                "explanation": f"同比下降 {abs(gross_margin_delta):.2f} 个百分点，需核验价格、成本和产品结构。",
            }
        )
    if not anomalies:
        anomalies.append(
            {
                "level": "灰",
                "title": "未触发预设异常阈值",
                "explanation": "这不代表没有风险，仍需结合附注、公告和行业变化核验。",
            }
        )
    operating = f"最近报告期单季营收同比 {_metric_text(revenue_yoy)}，归母利润同比 {_metric_text(profit_yoy)}。"
    quality = (
        f"经营现金流/利润为 {_metric_text(ocf_profit, '倍')}，"
        f"资产负债率为 {_metric_text((_float(latest.debt_ratio) * 100) if latest.debt_ratio is not None else None)}。"
    )
    chart = [
        {
            "period": item.report_date.isoformat(),
            "revenue": _float(item.revenue_single_quarter),
            "profit": _float(item.profit_single_quarter),
            "cash_flow": _float(item.cash_flow_single_quarter),
            "gross_margin_pct": (_float(item.gross_margin) * 100)
            if item.gross_margin is not None
            else None,
            "roe_pct": (_float(item.roe) * 100) if item.roe is not None else None,
            "debt_ratio_pct": (_float(item.debt_ratio) * 100)
            if item.debt_ratio is not None
            else None,
            "accounts_receivable": _float(item.accounts_receivable),
            "inventory": _float(item.inventory),
        }
        for item in rows
    ]
    return {
        "status": "success",
        "report_period": latest.report_date.isoformat(),
        "source": latest.source,
        "source_url": latest.source_url,
        "updated_at": latest.fetched_at.isoformat(),
        "operating_conclusion": operating,
        "quality_conclusion": quality,
        "conclusions": [operating, quality],
        "trends": trends,
        "anomalies": [
            {
                **item,
                "report_period": latest.report_date.isoformat(),
                "source": latest.source,
                "updated_at": latest.fetched_at.isoformat(),
            }
            for item in anomalies
        ],
        "chart": chart,
    }


def _risk_report(items: list[CompanyAnnouncement]) -> dict:
    relevant = [item for item in items if item.announcement_category != "其他公告"]
    return {
        "status": "success" if items else "unavailable",
        "message": None if items else "暂无可靠数据",
        "counts": {
            level: sum(item.risk_level == level for item in relevant)
            for level in ("红", "黄", "灰")
        },
        "items": [
            {
                "title": item.title,
                "category": item.announcement_category,
                "risk_level": item.risk_level,
                "published_date": item.published_date.isoformat(),
                "catalog_source": item.catalog_source,
                "exchange": item.exchange,
                "url": item.source_document_url or item.url,
                "catalog_url": item.url,
                "updated_at": item.fetched_at.isoformat(),
                "explanation": (
                    "需优先阅读原公告并核验影响范围。"
                    if item.risk_level == "红"
                    else "需要持续跟踪后续进展和实际影响。"
                    if item.risk_level == "黄"
                    else "作为背景信息保留，不单独推导风险结论。"
                ),
            }
            for item in sorted(relevant, key=lambda value: value.published_date, reverse=True)[:100]
        ],
    }


def _valuation_report(item: CompanyValuationSnapshot | None) -> dict:
    if item is None:
        return {"status": "unavailable", "message": "暂无可靠数据"}
    metrics = {
        "market_cap_100m": _float(item.market_cap),
        "pe_ttm": _float(item.pe_ttm),
        "pb": _float(item.pb),
        "ps_ttm": _float(item.ps_ttm),
        "pe_percentile_pct": (_float(item.pe_percentile) * 100)
        if item.pe_percentile is not None
        else None,
        "pb_percentile_pct": (_float(item.pb_percentile) * 100)
        if item.pb_percentile is not None
        else None,
        "ps_percentile_pct": (_float(item.ps_percentile) * 100)
        if item.ps_percentile is not None
        else None,
    }
    available = [
        f"PE(TTM) {_metric_text(metrics['pe_ttm'], '倍')}",
        f"PB {_metric_text(metrics['pb'], '倍')}",
        f"PS(TTM) {_metric_text(metrics['ps_ttm'], '倍')}",
    ]
    return {
        "status": "success",
        "trade_date": item.trade_date.isoformat(),
        "source": item.source,
        "source_url": item.source_url,
        "updated_at": item.fetched_at.isoformat(),
        "metrics": metrics,
        "industry_comparison": item.industry_comparison,
        "scenarios": item.implied_growth,
        "conclusion": "，".join(available)
        + "。历史分位和情景仅用于描述市场定价，不直接断言估值泡沫。",
    }


def _industry_report(profile: CompanyProfile | None, evidence: list[CompanyResearchEvidence]):
    grouped = {topic: [] for topic in (*TOPIC_KEYWORDS, "产业资讯")}
    for item in evidence:
        grouped.setdefault(item.topic, []).append(
            {
                "content": item.content,
                "information_type": item.information_type,
                "source_name": item.source_name,
                "source_url": item.source_url,
                "source_date": item.source_date.isoformat() if item.source_date else None,
                "updated_at": item.fetched_at.isoformat(),
            }
        )
    timeline = sorted(
        [value for values in grouped.values() for value in values if value["source_date"]],
        key=lambda value: value["source_date"],
        reverse=True,
    )[:100]
    validation = [
        "后续季度营收和利润增速是否与产业叙事一致",
        "毛利率、经营现金流和应收账款是否同步改善",
        "研发投入、产能建设和客户进展是否有公告或年报证据",
    ]
    invalidation = [
        "产业观点持续出现但财务指标和订单证据未验证",
        "主要客户、产品价格或毛利率出现持续不利变化",
        "监管公告、审计意见或现金流质量与公司表态矛盾",
    ]
    return {
        "status": "success" if profile or evidence else "unavailable",
        "message": None if profile or evidence else "暂无可靠数据",
        "summary": (
            f"公司所属行业为{profile.industry or '暂无可靠数据'}。"
            f"主营业务：{profile.main_business or '暂无可靠数据'}"
            if profile
            else "暂无可靠数据"
        ),
        "evidence_by_topic": grouped,
        "timeline": timeline,
        "validation_indicators": validation,
        "invalidation_conditions": invalidation,
    }


def build_company_report(db: Session, symbol: str) -> dict:
    profile = db.scalar(select(CompanyProfile).where(CompanyProfile.symbol == symbol))
    financials = db.scalars(
        select(CompanyFinancialPeriod)
        .where(CompanyFinancialPeriod.symbol == symbol)
        .order_by(CompanyFinancialPeriod.report_date)
    ).all()
    announcements = db.scalars(
        select(CompanyAnnouncement)
        .where(CompanyAnnouncement.symbol == symbol)
        .order_by(CompanyAnnouncement.published_date.desc())
    ).all()
    valuation = db.scalar(
        select(CompanyValuationSnapshot)
        .where(CompanyValuationSnapshot.symbol == symbol)
        .order_by(CompanyValuationSnapshot.trade_date.desc())
    )
    evidence = db.scalars(
        select(CompanyResearchEvidence)
        .where(CompanyResearchEvidence.symbol == symbol)
        .order_by(CompanyResearchEvidence.source_date.desc())
    ).all()
    financial_report = _financial_report(financials)
    risk_report = _risk_report(announcements)
    valuation_report = _valuation_report(valuation)
    industry_report = _industry_report(profile, evidence)
    latest_updates = [
        value
        for value in (
            profile.fetched_at if profile else None,
            max((item.fetched_at for item in financials), default=None),
            max((item.fetched_at for item in announcements), default=None),
            valuation.fetched_at if valuation else None,
            max((item.fetched_at for item in evidence), default=None),
        )
        if value
    ]
    return {
        "symbol": symbol,
        "company": {
            "name": profile.name if profile else symbol,
            "industry": profile.industry if profile else None,
            "market": profile.market if profile else None,
            "source": profile.source if profile else None,
            "source_url": profile.source_url if profile else None,
        },
        "updated_at": max(latest_updates).isoformat() if latest_updates else None,
        "financial": financial_report,
        "risk_radar": risk_report,
        "valuation": valuation_report,
        "industry_logic": industry_report,
        "data_notice": "数据缺失处统一显示“暂无可靠数据”；系统不会使用 AI 补造财务、公告或产业事实。",
        "developer": {
            "financial_period_count": len(financials),
            "announcement_count": len(announcements),
            "evidence_count": len(evidence),
            "has_valuation": valuation is not None,
        },
    }
