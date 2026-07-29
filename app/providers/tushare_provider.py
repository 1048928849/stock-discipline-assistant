from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.config import Settings
from app.data_hub.contracts import (
    AnnouncementProvider,
    DailyBar,
    FundamentalDataProvider,
    IndustryConceptProvider,
    MarketDataProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    shanghai_today,
    to_shanghai_aware,
)
from app.domain.market_symbols import CSI300_INTERNAL_SYMBOL


class TushareProvider(
    MarketDataProvider,
    FundamentalDataProvider,
    AnnouncementProvider,
    IndustryConceptProvider,
):
    """可启用的Tushare适配器；未配置Token时不会发起请求，也不会视为故障。"""

    def __init__(
        self,
        settings: Settings,
        *,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.settings = settings
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.metadata = ProviderMetadata(
            provider_id="tushare",
            supported_capabilities=(
                "market.daily.unadjusted",
                "market.quote.latest_close",
                "fundamental.profile",
                "fundamental.statements",
                "fundamental.valuation",
                "announcement.catalog",
                "industry.membership",
            ),
            required_credentials=("TUSHARE_TOKEN",),
            enabled=settings.tushare_enabled,
            priority=settings.tushare_priority,
            health_status="not_configured" if not settings.tushare_token else "unknown",
            realtime_supported=False,
            timeout=settings.provider_timeout_seconds,
            retry=settings.provider_max_retries,
            rate_limit="Tushare账户权限决定",
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.tushare_token)

    def _pro(self):
        if not self.metadata.enabled:
            raise ProviderUnavailableError("TushareProvider已禁用")
        if not self.configured:
            raise ProviderUnavailableError("未配置TUSHARE_TOKEN，已安全跳过Tushare")
        try:
            import tushare as ts
        except ImportError as exc:
            raise ProviderUnavailableError("未安装tushare包，已安全跳过Tushare") from exc
        return ts.pro_api(self.settings.tushare_token)

    @staticmethod
    def _code(symbol: str) -> str:
        suffix = "SH" if symbol.startswith(("5", "6", "9")) else "BJ" if symbol[0] in "48" else "SZ"
        return f"{symbol}.{suffix}"

    @staticmethod
    def _records(frame) -> list[dict]:
        return [] if frame is None or frame.empty else frame.where(frame.notna(), None).to_dict("records")

    def health_check(self, probe: bool = False) -> dict[str, Any]:
        if not self.metadata.enabled:
            return {"status": "disabled", "message": "已禁用"}
        if not self.configured:
            return {"status": "not_configured", "message": "未配置Token，调用时安全跳过"}
        if not probe:
            return {"status": "configured", "message": "凭据已配置，尚未主动探测"}
        try:
            rows = self._pro().trade_cal(
                exchange="SSE", start_date=shanghai_today().strftime("%Y%m%d")
            )
            return {"status": "healthy", "message": f"健康检查成功，返回{len(rows)}行"}
        except Exception as exc:
            return {"status": "unhealthy", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def get_quote(self, symbol: str) -> Quote:
        raw_fetched_at = self.now_fn()
        fetched_at = to_shanghai_aware(
            raw_fetched_at,
            naive_is_shanghai=raw_fetched_at.tzinfo is None,
        )
        expected_session = self.calendar.latest_completed_session(fetched_at)
        frame = self._pro().daily(
            ts_code=self._code(symbol),
            trade_date=expected_session.strftime("%Y%m%d"),
        )
        rows = self._records(frame)
        if not rows:
            raise ProviderUnavailableError("Tushare免费日线没有当日行情；该Provider不冒充实时行情")
        row = rows[0]
        try:
            trade_date = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
            observed_at = self.calendar.session_close_at(trade_date)
            price = Decimal(str(row["close"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(
                "Tushare daily close has invalid market semantics"
            ) from exc
        return Quote(
            symbol=symbol,
            name=symbol,
            price=price,
            quote_type="latest_close",
            observed_at=observed_at,
            price_unit="CNY",
            source="tushare_daily_close",
            source_api="daily",
            fetched_at=fetched_at,
        )

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        frame = self._pro().daily(
            ts_code=self._code(symbol),
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
        rows = []
        raw_fetched_at = self.now_fn()
        fetched_at = to_shanghai_aware(
            raw_fetched_at,
            naive_is_shanghai=raw_fetched_at.tzinfo is None,
        )
        for row in reversed(self._records(frame)):
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=datetime.strptime(str(row["trade_date"]), "%Y%m%d").date(),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row["vol"])) * 100,
                    adjustment="unadjusted",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=self.calendar.session_close_at(
                        datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
                    ),
                    source="tushare_daily_unadjusted",
                    fetched_at=fetched_at,
                )
            )
        if not rows:
            raise ProviderUnavailableError("Tushare未返回日线数据")
        return rows

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        code = "000300.SH" if symbol == CSI300_INTERNAL_SYMBOL else symbol
        frame = self._pro().index_daily(
            ts_code=code, start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
        rows = [
            {
                "date": datetime.strptime(str(row["trade_date"]), "%Y%m%d").date(),
                "close": float(row["close"]),
                "volume": float(row["vol"]) * 100,
            }
            for row in reversed(self._records(frame))
        ]
        if not rows:
            raise ProviderUnavailableError("Tushare未返回指数数据")
        raw_fetched_at = self.now_fn()
        return {
            "rows": rows,
            "source": "Tushare/index_daily",
            "fetched_at": to_shanghai_aware(
                raw_fetched_at,
                naive_is_shanghai=raw_fetched_at.tzinfo is None,
            ),
        }

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        raise ProviderUnavailableError("Tushare行业名称需要先映射指数代码，当前免费配置安全跳过")

    def company_profile(self, symbol: str) -> dict:
        rows = self._records(self._pro().stock_company(ts_code=self._code(symbol)))
        if not rows:
            raise ProviderUnavailableError("Tushare未返回公司概况")
        row = rows[0]
        return {
            "A股简称": row.get("com_name") or symbol,
            "公司名称": row.get("com_name"),
            "所属市场": row.get("exchange"),
            "主营业务": row.get("main_business"),
            "经营范围": row.get("business_scope"),
            "官方网站": row.get("website"),
        }

    def financial_statements(self, symbol: str) -> dict[str, list[dict]]:
        pro, code = self._pro(), self._code(symbol)
        kwargs = {"ts_code": code, "limit": 16}
        balance = self._records(pro.balancesheet(**kwargs))
        income = self._records(pro.income(**kwargs))
        cash_flow = self._records(pro.cashflow(**kwargs))
        return {
            "balance": [
                {
                    "报告日": row.get("end_date"),
                    "资产总计": row.get("total_assets"),
                    "负债合计": row.get("total_liab"),
                    "归属于母公司股东权益合计": row.get("total_hldr_eqy_exc_min_int"),
                    "应收账款": row.get("accounts_receiv"),
                    "存货": row.get("inventories"),
                }
                for row in balance
            ],
            "income": [
                {
                    "报告日": row.get("end_date"),
                    "营业收入": row.get("total_revenue") or row.get("revenue"),
                    "营业成本": row.get("oper_cost"),
                    "净利润": row.get("n_income"),
                    "归属于母公司所有者的净利润": row.get("n_income_attr_p"),
                    "研发费用": row.get("rd_exp"),
                }
                for row in income
            ],
            "cash_flow": [
                {
                    "报告日": row.get("end_date"),
                    "经营活动产生的现金流量净额": row.get("n_cashflow_act"),
                }
                for row in cash_flow
            ],
        }

    def valuation_history(self, symbol: str) -> dict[str, list[dict]]:
        rows = self._records(self._pro().daily_basic(ts_code=self._code(symbol), limit=1000))
        result: dict[str, list[dict]] = {"market_cap": [], "pe_ttm": [], "pb": []}
        for row in reversed(rows):
            day = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date().isoformat()
            result["market_cap"].append({"date": day, "value": row.get("total_mv")})
            result["pe_ttm"].append({"date": day, "value": row.get("pe_ttm")})
            result["pb"].append({"date": day, "value": row.get("pb")})
        return result

    def valuation_comparison(self, symbol: str) -> list[dict]:
        raise ProviderUnavailableError("Tushare同行估值需要行业成分组合查询，当前未启用付费路径")

    def company_announcements(self, symbol: str, start: date, end: date) -> list[dict]:
        raise ProviderUnavailableError("Tushare公告属于独立权限，未配置时安全跳过")

    def company_industry_concepts(self, symbol: str) -> dict:
        rows = self._records(self._pro().index_member_all(ts_code=self._code(symbol), is_new="Y"))
        return {"industries": rows, "concepts": [], "source": "Tushare/申万行业成分"}
