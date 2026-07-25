from datetime import date, datetime, timedelta, timezone

from app.data_hub.trading_calendar import SHANGHAI_TZ
from app.models import (
    CompanyAnnouncement,
    CompanyFinancialPeriod,
    CompanyProfile,
    CompanyResearchRefresh,
    CompanyValuationSnapshot,
)

from app.services.company_research import (
    build_company_report,
    classify_announcement,
    extract_report_evidence,
    refresh_company_research_if_needed,
    sync_company_research,
)


COMPANIES = {
    "600519": ("贵州茅台", "食品饮料制造业", "白酒生产与销售"),
    "000001": ("平安银行", "货币金融服务", "商业银行业务"),
    "300502": ("新易盛", "通信设备制造业", "光模块研发、生产与销售"),
}


def financial_fixture(symbol: str):
    factor = 1 + int(symbol[-1]) / 10
    periods = []
    for year in (2023, 2024, 2025):
        for month in (3, 6, 9, 12):
            periods.append(date(year, month, 31 if month in (3, 12) else 30))
    balance, income, cash_flow = [], [], []
    cumulative_revenue = cumulative_cost = cumulative_profit = cumulative_cash = 0
    current_year = None
    for index, period in enumerate(periods):
        if current_year != period.year:
            current_year = period.year
            cumulative_revenue = cumulative_cost = cumulative_profit = cumulative_cash = 0
        single_revenue = factor * (100_000_000 + index * 5_000_000)
        single_cost = single_revenue * 0.6
        single_profit = single_revenue * 0.16
        single_cash = single_profit * (0.9 + (index % 3) * 0.1)
        cumulative_revenue += single_revenue
        cumulative_cost += single_cost
        cumulative_profit += single_profit
        cumulative_cash += single_cash
        report = period.strftime("%Y%m%d")
        balance.append(
            {
                "报告日": report,
                "资产总计": factor * (2_000_000_000 + index * 100_000_000),
                "负债合计": factor * (700_000_000 + index * 30_000_000),
                "归属于母公司股东权益合计": factor * (1_300_000_000 + index * 70_000_000),
                "应收账款": factor * (150_000_000 + index * 8_000_000),
                "存货": factor * (180_000_000 + index * 6_000_000),
            }
        )
        income.append(
            {
                "报告日": report,
                "营业收入": cumulative_revenue,
                "营业成本": cumulative_cost,
                "净利润": cumulative_profit,
                "归属于母公司所有者的净利润": cumulative_profit,
                "研发费用": cumulative_revenue * 0.05,
            }
        )
        cash_flow.append({"报告日": report, "经营活动产生的现金流量净额": cumulative_cash})
    return {"balance": balance, "income": income, "cash_flow": cash_flow}


class FakeResearchProvider:
    def company_profile(self, symbol):
        name, industry, business = COMPANIES.get(symbol, (f"同行{symbol}", "同行业", "同类业务"))
        return {
            "A股简称": name,
            "公司名称": name,
            "细分行业": industry,
            "所属市场": "上交所" if symbol.startswith("6") else "深交所",
            "主营业务": business,
            "经营范围": business,
            "官方网站": "https://example.invalid",
        }

    def financial_statements(self, symbol):
        return financial_fixture(symbol)

    def company_announcements(self, symbol, start, end):
        self.announcement_window = (start, end)
        return [
            {
                "公告标题": f"{symbol} 2025年年度报告",
                "公告日期": "2026-03-30",
                "网址": f"https://example.invalid/{symbol}/annual.pdf",
                "目录来源": "巨潮资讯",
            },
            {
                "公告标题": f"{symbol} 关于股东减持计划的公告",
                "公告日期": "2026-06-01",
                "网址": f"https://example.invalid/{symbol}/reduction.pdf",
                "目录来源": "上交所" if symbol.startswith("6") else "深交所",
            },
            {
                "公告标题": f"{symbol} 监管问询函回复公告",
                "公告日期": "2026-06-15",
                "网址": f"https://example.invalid/{symbol}/inquiry.pdf",
                "目录来源": "巨潮资讯",
            },
        ]

    def valuation_history(self, symbol):
        start = date(2023, 1, 1)
        return {
            "market_cap": [
                {"date": (start + timedelta(days=i * 30)).isoformat(), "value": 1000 + i * 10}
                for i in range(44)
            ],
            "pe_ttm": [
                {"date": (start + timedelta(days=i * 30)).isoformat(), "value": 20 + i / 2}
                for i in range(44)
            ],
            "pb": [
                {"date": (start + timedelta(days=i * 30)).isoformat(), "value": 2 + i / 20}
                for i in range(44)
            ],
        }

    def valuation_comparison(self, symbol):
        return [
            {
                "代码": symbol,
                "简称": self.company_profile(symbol)["A股简称"],
                "市盈率-TTM": 35,
                "市净率-MRQ": 4,
                "市销率-TTM": 6,
            },
            {"代码": "行业中值", "简称": "行业中值", "市盈率-TTM": 25, "市净率-MRQ": 3},
            {"代码": "600345", "简称": "同行甲", "市盈率-TTM": 22, "市净率-MRQ": 2.5},
            {"代码": "603083", "简称": "同行乙", "市盈率-TTM": 28, "市净率-MRQ": 3.2},
            {"代码": "600487", "简称": "同行丙", "市盈率-TTM": 31, "市净率-MRQ": 3.8},
        ]


class FailIfCalledProvider(FakeResearchProvider):
    def company_profile(self, symbol):
        raise AssertionError("fresh Research cache must not refresh")


def test_legacy_research_uses_injected_window_utc_storage_and_aware_output(session):
    evaluated_at = datetime(2026, 7, 25, 9, 30, tzinfo=SHANGHAI_TZ)
    provider = FakeResearchProvider()
    result = sync_company_research(
        session,
        "300502",
        provider=provider,
        include_documents=False,
        evaluated_at=evaluated_at,
    )
    expected_storage = datetime(2026, 7, 25, 1, 30)
    expected_end = date(2026, 7, 25)
    expected_start = expected_end - timedelta(days=3 * 366)
    assert provider.announcement_window == (expected_start, expected_end)
    assert datetime.fromisoformat(result["updated_at"]) == evaluated_at
    assert session.query(CompanyProfile).one().fetched_at == expected_storage
    assert all(
        row.fetched_at == expected_storage
        for model in (
            CompanyFinancialPeriod,
            CompanyAnnouncement,
            CompanyValuationSnapshot,
        )
        for row in session.query(model).all()
    )
    refresh = session.query(CompanyResearchRefresh).filter_by(
        symbol="300502", section="announcements"
    ).one()
    assert refresh.last_attempt_at == expected_storage
    assert refresh.scan_start == datetime.combine(expected_start, datetime.min.time())
    assert refresh.scan_end == datetime.combine(expected_end, datetime.max.time())
    assert refresh.scan_start != refresh.fetched_at
    for field in (
        "last_attempt_at",
        "last_success_at",
        "stale_after",
        "observed_at",
        "fetched_at",
        "checked_at",
        "scan_start",
        "scan_end",
    ):
        value = getattr(refresh, field)
        assert value is None or value.tzinfo is None


def test_research_refresh_schedule_compares_against_utc_naive_storage(session):
    acquired_at = datetime(2026, 7, 25, 9, 30, tzinfo=SHANGHAI_TZ)
    sync_company_research(
        session,
        "300502",
        provider=FakeResearchProvider(),
        include_documents=False,
        evaluated_at=acquired_at,
    )
    session.add(
        CompanyValuationSnapshot(
            symbol="300502",
            trade_date=acquired_at.date(),
            source="test",
            fetched_at=datetime(2026, 7, 25, 1, 30),
        )
    )
    session.commit()
    checked_at = acquired_at + timedelta(minutes=1)
    result = refresh_company_research_if_needed(
        session,
        "300502",
        data_service=FailIfCalledProvider(),
        evaluated_at=checked_at.astimezone(timezone.utc),
    )
    assert result["status"] == "fresh"
    assert result["refreshed_sections"] == []
    assert datetime.fromisoformat(result["checked_at"]) == checked_at
    assert all(
        datetime.fromisoformat(item["updated_at"]).tzinfo is not None
        for item in result["sections"].values()
    )


def test_refresh_propagates_same_business_instant_to_sync(session):
    evaluated_at = datetime(2026, 7, 25, 1, 30, tzinfo=timezone.utc)
    provider = FakeResearchProvider()
    result = refresh_company_research_if_needed(
        session,
        "300502",
        force=True,
        data_service=provider,
        evaluated_at=evaluated_at,
    )
    assert provider.announcement_window[1] == date(2026, 7, 25)
    assert datetime.fromisoformat(result["updated_at"]) == evaluated_at.astimezone(
        SHANGHAI_TZ
    )


def test_three_industries_build_complete_company_research(session):
    provider = FakeResearchProvider()
    for symbol, (name, industry, _) in COMPANIES.items():
        sync = sync_company_research(session, symbol, provider=provider, include_documents=False)
        assert sync["status"] == "success"
        report = build_company_report(session, symbol)
        assert report["company"]["name"] == name
        assert report["company"]["industry"] == industry
        assert report["financial"]["status"] == "success"
        assert len(report["financial"]["chart"]) == 12
        assert report["financial"]["report_period"] == "2025-12-31"
        assert report["financial"]["source"]
        assert report["financial"]["updated_at"]
        assert all(item["explanation"] for item in report["financial"]["trends"])
        assert report["risk_radar"]["items"]
        assert all(item["url"].startswith("https://") for item in report["risk_radar"]["items"])
        assert report["valuation"]["metrics"]["pe_ttm"] is not None
        assert set(report["valuation"]["scenarios"]) == {"乐观", "中性", "悲观"}
        assert report["valuation"]["industry_comparison"]["peer_operating"]
        assert report["industry_logic"]["evidence_by_topic"]["主营业务"]
        assert report["industry_logic"]["validation_indicators"]
        assert report["industry_logic"]["invalidation_conditions"]


def test_company_research_api_and_missing_data_are_user_readable(client, session):
    empty = client.get("/api/v1/company-research/600000")
    assert empty.status_code == 200
    assert empty.json()["financial"]["message"] == "暂无可靠数据"

    sync_company_research(
        session, "600519", provider=FakeResearchProvider(), include_documents=False
    )
    result = client.get("/api/v1/company-research/600519")
    assert result.status_code == 200
    assert result.json()["company"]["name"] == "贵州茅台"
    page = client.get("/company-research")
    assert page.status_code == 200
    assert "公司研究中心" in page.text
    assert "开发者详情（原始 JSON）" in page.text


def test_announcement_categories_and_annual_report_evidence_extraction():
    cases = {
        "2025年年度报告": "年报",
        "业绩预告": "业绩预告",
        "股东减持计划": "减持",
        "股份质押公告": "质押",
        "重大诉讼进展": "诉讼",
        "监管问询函": "监管问询",
        "对外担保": "担保",
        "限售股份上市流通": "解禁",
        "非标准审计意见": "审计意见",
    }
    for title, expected in cases.items():
        category, level = classify_announcement(title)
        assert category == expected
        assert level in {"红", "黄", "灰"}

    assert classify_announcement("年报信息披露重大差错责任追究制度")[0] == "其他公告"
    assert classify_announcement("2025年半年度报告")[0] == "其他公告"

    evidence = extract_report_evidence(
        "公司主营业务为设备制造。上游供应商提供核心原料，主要客户较为集中。"
        "新增产能正在建设，研发投入持续增长，这些构成公司的核心竞争力。"
    )
    topics = {item["topic"] for item in evidence}
    assert {"主营业务", "上下游", "主要客户", "产能", "研发", "竞争优势"}.issubset(topics)
