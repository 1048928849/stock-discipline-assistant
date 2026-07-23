import time
from datetime import date, datetime
from decimal import Decimal

from app.providers.base import (
    AnnouncementProvider,
    FundamentalDataProvider,
    IndustryConceptProvider,
    MarketDataProvider,
    NewsProvider,
    ProviderMetadata,
)
from app.providers.market import DailyBar, ProviderUnavailableError, Quote


class AKShareProvider(
    MarketDataProvider,
    FundamentalDataProvider,
    AnnouncementProvider,
    IndustryConceptProvider,
    NewsProvider,
):
    """AKShare 适配器，按东方财富→腾讯→新浪顺序自动故障切换。"""

    source = "akshare"

    def __init__(self, retries: int = 2, timeout: float = 20):
        self.retries = max(1, retries)
        self.timeout = timeout
        self.metadata = ProviderMetadata(
            provider_id="akshare",
            supported_capabilities=(
                "market.quote",
                "market.daily",
                "market.index_daily",
                "market.sector_daily",
                "market.symbols",
                "market.indices",
                "market.sectors",
                "fundamental.profile",
                "fundamental.statements",
                "fundamental.valuation",
                "announcement.catalog",
                "announcement.daily",
                "industry.membership",
                "news.company",
            ),
            enabled=True,
            priority=50,
            health_status="unknown",
            realtime_supported=True,
            timeout=timeout,
            retry=self.retries,
            rate_limit="公开接口限制，应用内有限重试",
        )

    def health_check(self, probe: bool = False) -> dict:
        try:
            ak = self._ak()
            if not probe:
                return {
                    "status": "available",
                    "message": f"AKShare {getattr(ak, '__version__', 'unknown')} 已安装",
                }
            frame = ak.stock_info_a_code_name()
            return {"status": "healthy", "message": f"健康检查成功，返回{len(frame)}行"}
        except Exception as exc:
            return {"status": "unhealthy", "message": f"{type(exc).__name__}: {str(exc)[:160]}"}

    @staticmethod
    def _ak():
        try:
            import akshare as ak

            return ak
        except ImportError as exc:
            raise ProviderUnavailableError("AKShare 未安装") from exc

    @staticmethod
    def _market_symbol(symbol: str) -> str:
        if symbol.startswith(("4", "8")):
            return f"bj{symbol}"
        if symbol.startswith(("5", "6", "9")):
            return f"sh{symbol}"
        return f"sz{symbol}"

    def _retry(self, name: str, callback):
        errors = []
        for attempt in range(1, self.retries + 1):
            try:
                return callback()
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:240]
                errors.append(f"{name}第{attempt}次：{type(exc).__name__}({detail})")
                if attempt < self.retries:
                    time.sleep(0.3 * attempt)
        raise RuntimeError("；".join(errors))

    @staticmethod
    def _quote_from_frame(frame, symbol: str, source: str, api_name: str) -> Quote:
        required = {"代码", "名称", "最新价"}
        if not required.issubset(frame.columns):
            raise ProviderUnavailableError(
                f"{api_name} 返回结构变化，缺少字段：{required - set(frame.columns)}"
            )
        codes = frame["代码"].astype(str).str[-6:].str.zfill(6)
        rows = frame.loc[codes == symbol]
        if rows.empty:
            raise ProviderUnavailableError(f"{api_name} 未找到股票代码 {symbol}")
        row = rows.iloc[0]
        return Quote(
            symbol=symbol,
            name=str(row["名称"]),
            price=Decimal(str(row["最新价"])),
            source=source,
            source_api=api_name,
            fetched_at=datetime.now(),
        )

    def get_quote(self, symbol: str) -> Quote:
        ak = self._ak()
        providers = (
            (
                "东方财富",
                lambda: self._quote_from_frame(
                    ak.stock_zh_a_spot_em(),
                    symbol,
                    "akshare_eastmoney",
                    "stock_zh_a_spot_em",
                ),
            ),
            (
                "新浪",
                lambda: self._quote_from_frame(
                    ak.stock_zh_a_spot(),
                    symbol,
                    "akshare_sina",
                    "stock_zh_a_spot",
                ),
            ),
        )
        errors = []
        for name, callback in providers:
            try:
                return self._retry(name, callback)
            except Exception as exc:
                errors.append(str(exc))
        raise ProviderUnavailableError("实时行情全部数据源失败：" + "；".join(errors))

    @staticmethod
    def _history_rows(
        frame,
        symbol: str,
        columns: dict[str, str],
        source: str,
        volume_multiplier: int = 1,
    ) -> list[DailyBar]:
        required = set(columns.values())
        if not required.issubset(frame.columns):
            raise ProviderUnavailableError(
                f"{source} 返回结构变化，缺少字段：{required - set(frame.columns)}"
            )
        fetched_at = datetime.now()
        rows = []
        for _, row in frame.iterrows():
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=date.fromisoformat(str(row[columns["date"]])[:10]),
                    open=Decimal(str(row[columns["open"]])),
                    high=Decimal(str(row[columns["high"]])),
                    low=Decimal(str(row[columns["low"]])),
                    close=Decimal(str(row[columns["close"]])),
                    volume=Decimal(str(row[columns["volume"]])) * volume_multiplier,
                    source=source,
                    fetched_at=fetched_at,
                )
            )
        if not rows:
            raise ProviderUnavailableError(f"{source} 未返回 {symbol} 的历史数据")
        return rows

    def get_history(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        ak = self._ak()
        prefixed = self._market_symbol(symbol)
        start_text, end_text = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        providers = (
            (
                "东方财富",
                lambda: self._history_rows(
                    ak.stock_zh_a_hist(
                        symbol=symbol,
                        period="daily",
                        start_date=start_text,
                        end_date=end_text,
                        adjust="qfq",
                        timeout=self.timeout,
                    ),
                    symbol,
                    {
                        "date": "日期",
                        "open": "开盘",
                        "high": "最高",
                        "low": "最低",
                        "close": "收盘",
                        "volume": "成交量",
                    },
                    "akshare_eastmoney_qfq",
                ),
            ),
            (
                "腾讯",
                lambda: self._history_rows(
                    ak.stock_zh_a_hist_tx(
                        symbol=prefixed,
                        start_date=start_text,
                        end_date=end_text,
                        adjust="qfq",
                        timeout=self.timeout,
                    ),
                    symbol,
                    {
                        "date": "date",
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "close",
                        "volume": "amount",
                    },
                    "akshare_tencent_qfq",
                    volume_multiplier=100,
                ),
            ),
            (
                "新浪",
                lambda: self._history_rows(
                    ak.stock_zh_a_daily(
                        symbol=prefixed,
                        start_date=start_text,
                        end_date=end_text,
                        adjust="qfq",
                    ),
                    symbol,
                    {
                        "date": "date",
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "close",
                        "volume": "volume",
                    },
                    "akshare_sina_qfq",
                ),
            ),
        )
        errors = []
        for name, callback in providers:
            try:
                return self._retry(name, callback)
            except Exception as exc:
                errors.append(str(exc))
        raise ProviderUnavailableError("前复权历史行情全部数据源失败：" + "；".join(errors))

    @staticmethod
    def _benchmark_rows(frame, source: str) -> dict:
        aliases = {
            "date": ("日期", "date"),
            "close": ("收盘", "close"),
            "volume": ("成交量", "volume"),
        }
        selected = {}
        for target, names in aliases.items():
            selected[target] = next((name for name in names if name in frame.columns), None)
        if not selected["date"] or not selected["close"]:
            raise ProviderUnavailableError(f"{source} 返回结构变化，缺少日期或收盘字段")
        rows = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "date": date.fromisoformat(str(row[selected["date"]])[:10]),
                    "close": float(row[selected["close"]]),
                    "volume": float(row[selected["volume"]]) if selected["volume"] else None,
                }
            )
        if len(rows) < 20:
            raise ProviderUnavailableError(f"{source} 历史数据不足20个交易日")
        rows.sort(key=lambda item: str(item["date"]))
        return {"rows": rows, "source": source, "fetched_at": datetime.now()}

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        errors = []
        providers = (
            (
                "东方财富指数",
                lambda: self._ak().stock_zh_index_daily_em(
                    symbol=symbol,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
                f"AKShare/东方财富指数 {symbol}",
            ),
            (
                "东方财富指数备用",
                lambda: self._ak().index_zh_a_hist(
                    symbol=symbol[-6:],
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
                f"AKShare/index_zh_a_hist {symbol[-6:]}",
            ),
        )
        for name, callback, source in providers:
            try:
                return self._benchmark_rows(self._retry(name, callback), source)
            except Exception as exc:
                errors.append(str(exc))
        raise ProviderUnavailableError("宽基指数数据暂不可用：" + "；".join(errors))

    def get_sector_history(self, industry: str, start: date, end: date) -> dict:
        try:
            frame = self._retry(
                "东方财富行业板块",
                lambda: self._ak().stock_board_industry_hist_em(
                    symbol=industry,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    period="日k",
                    adjust="qfq",
                ),
            )
            return self._benchmark_rows(frame, f"AKShare/东方财富行业板块 {industry}")
        except Exception as exc:
            raise ProviderUnavailableError(f"行业指数数据暂不可用：{exc}") from exc

    @staticmethod
    def _records(frame, limit: int = 500) -> list[dict]:
        import pandas as pd

        cleaned = frame.head(limit).astype(object).where(pd.notna(frame.head(limit)), None)
        return [
            {
                str(key): value.isoformat() if hasattr(value, "isoformat") else value
                for key, value in row.items()
            }
            for row in cleaned.to_dict("records")
        ]

    def list_symbols(self) -> list[dict]:
        try:
            frame = self._ak().stock_info_a_code_name()
            if not {"code", "name"}.issubset(frame.columns):
                raise ProviderUnavailableError("AKShare 股票列表返回结构变化")
            return self._records(frame.rename(columns={"code": "symbol"}), 10000)
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(f"股票列表暂不可用：{type(exc).__name__}") from exc

    def list_indices(self, family: str = "上证系列指数") -> list[dict]:
        try:
            return self._records(self._ak().stock_zh_index_spot_em(symbol=family))
        except Exception as exc:
            raise ProviderUnavailableError(f"指数行情暂不可用：{type(exc).__name__}") from exc

    def list_sectors(self) -> list[dict]:
        try:
            return self._records(self._ak().stock_board_industry_name_em())
        except Exception as exc:
            raise ProviderUnavailableError(f"行业板块暂不可用：{type(exc).__name__}") from exc

    def company_industry_concepts(self, symbol: str) -> dict:
        profile = self.company_profile(symbol)
        industry = profile.get("细分行业") or profile.get("所属行业")
        return {
            "industries": [{"name": industry, "level": "provider"}] if industry else [],
            "concepts": [],
            "source": "AKShare/巨潮公司概况",
        }

    def company_news(self, symbol: str, start: datetime, end: datetime) -> list[dict]:
        try:
            frame = self._ak().stock_news_em(symbol=symbol)
            rows = self._records(frame, 100)
            return [
                row
                for row in rows
                if start.date().isoformat()
                <= str(row.get("发布时间") or row.get("日期") or end.date())[:10]
                <= end.date().isoformat()
            ]
        except Exception as exc:
            raise ProviderUnavailableError(f"公开新闻暂不可用：{type(exc).__name__}") from exc

    def announcements(self, symbol: str, day: date) -> list[dict]:
        try:
            frame = self._ak().stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
            code_column = next(
                (name for name in ("代码", "股票代码") if name in frame.columns), None
            )
            if code_column:
                frame = frame.loc[frame[code_column].astype(str).str.zfill(6) == symbol]
            return self._records(frame)
        except Exception as exc:
            raise ProviderUnavailableError(f"公告数据暂不可用：{type(exc).__name__}") from exc

    def company_profile(self, symbol: str) -> dict:
        try:
            frame = self._ak().stock_profile_cninfo(symbol=symbol)
            if frame.empty:
                raise ProviderUnavailableError(f"未找到 {symbol} 的公司概况")
            profile = self._records(frame, 1)[0]
            try:
                industry = self._ak().stock_industry_change_cninfo(
                    symbol=symbol, start_date="19900101", end_date=date.today().strftime("%Y%m%d")
                )
                if not industry.empty:
                    import pandas as pd

                    latest = industry.sort_values("变更日期").iloc[-1]
                    profile["细分行业"] = next(
                        (
                            latest.get(name)
                            for name in ("行业中类", "行业大类", "行业门类")
                            if pd.notna(latest.get(name)) and str(latest.get(name)).strip()
                        ),
                        None,
                    )
            except Exception:
                profile["细分行业"] = None
            return profile
        except ProviderUnavailableError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(f"公司概况暂不可用：{type(exc).__name__}") from exc

    def financial_statements(self, symbol: str) -> dict[str, list[dict]]:
        prefixed = self._market_symbol(symbol)
        statement_names = {
            "balance": "资产负债表",
            "income": "利润表",
            "cash_flow": "现金流量表",
        }
        result = {}
        errors = []
        for key, statement_name in statement_names.items():
            try:
                frame = self._ak().stock_financial_report_sina(
                    stock=prefixed, symbol=statement_name
                )
                result[key] = self._records(frame, 60)
            except Exception as exc:
                errors.append(f"{statement_name}:{type(exc).__name__}")
        if len(result) != 3:
            raise ProviderUnavailableError("三大财务报表不完整：" + "；".join(errors))
        return result

    def company_announcements(self, symbol: str, start: date, end: date) -> list[dict]:
        try:
            frame = self._ak().stock_zh_a_disclosure_report_cninfo(
                symbol=symbol,
                market="沪深京",
                keyword="",
                category="",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
            )
            records = self._records(frame, 1000)
            for item in records:
                item["目录来源"] = "巨潮资讯"
            return records
        except Exception as cninfo_exc:
            try:
                frame = self._ak().stock_individual_notice_report(
                    security=symbol,
                    symbol="全部",
                    begin_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                )
                records = self._records(frame, 1000)
                for item in records:
                    item["目录来源"] = "交易所公告公开目录（备用）"
                return records
            except Exception as fallback_exc:
                raise ProviderUnavailableError(
                    "公告目录暂不可用："
                    f"巨潮资讯 {type(cninfo_exc).__name__}；"
                    f"交易所备用目录 {type(fallback_exc).__name__}"
                ) from fallback_exc

    def valuation_history(self, symbol: str) -> dict[str, list[dict]]:
        indicators = {
            "market_cap": "总市值",
            "pe_ttm": "市盈率(TTM)",
            "pb": "市净率",
        }
        result = {}
        for key, indicator in indicators.items():
            try:
                frame = self._ak().stock_zh_valuation_baidu(
                    symbol=symbol, indicator=indicator, period="近三年"
                )
                result[key] = self._records(frame, 1500)
            except Exception:
                result[key] = []
        if not any(result.values()):
            raise ProviderUnavailableError("历史估值数据暂不可用")
        return result

    def valuation_comparison(self, symbol: str) -> list[dict]:
        try:
            frame = self._ak().stock_zh_valuation_comparison_em(
                symbol=self._market_symbol(symbol).upper()
            )
            return self._records(frame, 100)
        except Exception as exc:
            raise ProviderUnavailableError(f"同行估值比较暂不可用：{type(exc).__name__}") from exc
