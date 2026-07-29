import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from app.data_hub.contracts import (
    AnnouncementProvider,
    DailyBar,
    FundamentalDataProvider,
    IndustryConstituent,
    IndustryDaily,
    IndustryConceptProvider,
    IntradayBar,
    MarketAmountDaily,
    MarketBreadthDaily,
    MarketDataProvider,
    NewsProvider,
    ProviderMetadata,
    ProviderUnavailableError,
    Quote,
    TurnoverDaily,
)
from app.data_hub.trading_calendar import (
    TradingCalendar,
    get_trading_calendar,
    shanghai_now,
    shanghai_today,
    to_shanghai_aware,
)
from app.domain.market_symbols import CSI300_INTERNAL_SYMBOL


_AKSHARE_CSI300_EASTMONEY_SYMBOL = "sh000300"
_AKSHARE_CSI300_FALLBACK_SYMBOL = "000300"


class AKShareProvider(
    MarketDataProvider,
    FundamentalDataProvider,
    AnnouncementProvider,
    IndustryConceptProvider,
    NewsProvider,
):
    """AKShare 适配器，按东方财富→腾讯→新浪顺序自动故障切换。"""

    source = "akshare"

    def __init__(
        self,
        retries: int = 2,
        timeout: float = 20,
        *,
        calendar: TradingCalendar | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.retries = max(1, retries)
        self.timeout = timeout
        self.calendar = calendar or get_trading_calendar()
        self.now_fn = now_fn or shanghai_now
        self.metadata = ProviderMetadata(
            provider_id="akshare",
            supported_capabilities=(
                "market.quote.realtime",
                "market.quote.latest_close",
                "market.daily.qfq",
                "market.intraday.60m",
                "market.turnover.daily",
                "market.breadth.daily",
                "market.amount.daily",
                "market.industry.daily",
                "market.industry.constituents",
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
                "company.concepts",
                "company.industry_chain",
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
    def _column(frame, *names: str) -> str:
        selected = next((name for name in names if name in frame.columns), None)
        if selected is None:
            raise ProviderUnavailableError(
                f"AKShare response is missing one of the required columns: {names}"
            )
        return selected

    @staticmethod
    def _optional_decimal(value) -> Decimal | None:
        if value in (None, "", "-"):
            return None
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        return result if result.is_finite() else None

    def _aware_now(self) -> datetime:
        raw = self.now_fn()
        return to_shanghai_aware(raw, naive_is_shanghai=raw.tzinfo is None)

    def _quote_from_frame(
        self, frame, symbol: str, source: str, api_name: str
    ) -> Quote:
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
        raw_fetched_at = self.now_fn()
        fetched_at = to_shanghai_aware(
            raw_fetched_at,
            naive_is_shanghai=raw_fetched_at.tzinfo is None,
        )
        if self.calendar.is_realtime_session(fetched_at):
            # Spot endpoints expose no exchange timestamp. During an active
            # session, request completion is the synchronous snapshot time.
            quote_type = "realtime"
            observed_at = fetched_at
        else:
            day_field = next(
                (
                    field
                    for field in ("日期", "date", "trade_date")
                    if field in frame.columns
                ),
                None,
            )
            if day_field is None:
                raise ProviderUnavailableError(
                    f"{api_name} cannot prove an after-hours close trade date"
                )
            try:
                trade_date = date.fromisoformat(str(row[day_field])[:10])
            except (TypeError, ValueError) as exc:
                raise ProviderUnavailableError(
                    f"{api_name} has an invalid after-hours close trade date"
                ) from exc
            expected = self.calendar.latest_completed_session(fetched_at)
            if trade_date != expected:
                raise ProviderUnavailableError(
                    f"{api_name} close date does not match latest completed session"
                )
            quote_type = "latest_close"
            observed_at = self.calendar.session_close_at(trade_date)
        try:
            price = Decimal(str(row["最新价"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(f"{api_name} has an invalid price") from exc
        return Quote(
            symbol=symbol,
            name=str(row["名称"]),
            price=price,
            quote_type=quote_type,
            observed_at=observed_at,
            price_unit="CNY",
            source=source,
            source_api=api_name,
            fetched_at=fetched_at,
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

    def _history_rows(
        self,
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
        raw_fetched_at = self.now_fn()
        fetched_at = to_shanghai_aware(
            raw_fetched_at,
            naive_is_shanghai=raw_fetched_at.tzinfo is None,
        )
        latest_completed = self.calendar.latest_completed_session(fetched_at)
        rows = []
        for _, row in frame.iterrows():
            trade_date = date.fromisoformat(str(row[columns["date"]])[:10])
            if trade_date > latest_completed:
                continue
            rows.append(
                DailyBar(
                    symbol=symbol,
                    trade_date=trade_date,
                    open=Decimal(str(row[columns["open"]])),
                    high=Decimal(str(row[columns["high"]])),
                    low=Decimal(str(row[columns["low"]])),
                    close=Decimal(str(row[columns["close"]])),
                    volume=Decimal(str(row[columns["volume"]])) * volume_multiplier,
                    adjustment="qfq",
                    price_unit="CNY",
                    volume_unit="share",
                    observed_at=self.calendar.session_close_at(trade_date),
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

    def get_intraday_60m(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[IntradayBar]:
        fetched_at = self._aware_now()
        start_at = to_shanghai_aware(start, naive_is_shanghai=start.tzinfo is None)
        end_at = to_shanghai_aware(end, naive_is_shanghai=end.tzinfo is None)
        try:
            frame = self._retry(
                "eastmoney-60m",
                lambda: self._ak().stock_zh_a_hist_min_em(
                    symbol=symbol,
                    start_date=start_at.strftime("%Y-%m-%d %H:%M:%S"),
                    end_date=end_at.strftime("%Y-%m-%d %H:%M:%S"),
                    period="60",
                    adjust="qfq",
                ),
            )
            time_column = self._column(frame, "\u65f6\u95f4", "datetime", "date")
            open_column = self._column(frame, "\u5f00\u76d8", "open")
            close_column = self._column(frame, "\u6536\u76d8", "close")
            high_column = self._column(frame, "\u6700\u9ad8", "high")
            low_column = self._column(frame, "\u6700\u4f4e", "low")
            volume_column = self._column(frame, "\u6210\u4ea4\u91cf", "volume")
            amount_column = next(
                (name for name in ("\u6210\u4ea4\u989d", "amount") if name in frame.columns),
                None,
            )
            turnover_column = next(
                (name for name in ("\u6362\u624b\u7387", "turnover") if name in frame.columns),
                None,
            )
            rows = []
            for _, raw in frame.iterrows():
                parsed = datetime.fromisoformat(str(raw[time_column]).replace("Z", "+00:00"))
                bar_end = to_shanghai_aware(parsed, naive_is_shanghai=parsed.tzinfo is None)
                if bar_end.time().isoformat(timespec="minutes") not in {
                    "10:30",
                    "11:30",
                    "14:00",
                    "15:00",
                }:
                    continue
                if bar_end > fetched_at or bar_end > end_at:
                    continue
                rows.append(
                    IntradayBar(
                        symbol=symbol,
                        trade_date=bar_end.date(),
                        bar_start=bar_end - timedelta(hours=1),
                        bar_end=bar_end,
                        open=Decimal(str(raw[open_column])),
                        high=Decimal(str(raw[high_column])),
                        low=Decimal(str(raw[low_column])),
                        close=Decimal(str(raw[close_column])),
                        volume=Decimal(str(raw[volume_column])),
                        amount=(
                            self._optional_decimal(raw[amount_column])
                            if amount_column
                            else None
                        ),
                        turnover_rate=(
                            self._optional_decimal(raw[turnover_column])
                            if turnover_column
                            else None
                        ),
                        adjustment="qfq",
                        price_unit="CNY",
                        volume_unit="share",
                        observed_at=bar_end,
                        source="akshare_eastmoney_60m_qfq",
                        fetched_at=fetched_at,
                        completed=True,
                    )
                )
        except Exception as exc:
            raise ProviderUnavailableError(
                f"60-minute bars are unavailable: {type(exc).__name__}"
            ) from exc
        if not rows:
            raise ProviderUnavailableError("no completed 60-minute bars are available")
        rows.sort(key=lambda item: item.bar_start)
        return rows

    def get_turnover_daily(
        self, symbol: str, start: date, end: date
    ) -> list[TurnoverDaily]:
        fetched_at = self._aware_now()
        try:
            frame = self._retry(
                "eastmoney-turnover",
                lambda: self._ak().stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="qfq",
                    timeout=self.timeout,
                ),
            )
            date_column = self._column(frame, "\u65e5\u671f", "date")
            turnover_column = self._column(frame, "\u6362\u624b\u7387", "turnover")
            amount_column = next(
                (name for name in ("\u6210\u4ea4\u989d", "amount") if name in frame.columns),
                None,
            )
            latest_completed = self.calendar.latest_completed_session(fetched_at)
            rows = []
            for _, raw in frame.iterrows():
                trade_date = date.fromisoformat(str(raw[date_column])[:10])
                if trade_date > latest_completed:
                    continue
                turnover = self._optional_decimal(raw[turnover_column])
                if turnover is None:
                    continue
                rows.append(
                    TurnoverDaily(
                        symbol=symbol,
                        trade_date=trade_date,
                        turnover_rate=turnover,
                        amount=(
                            self._optional_decimal(raw[amount_column])
                            if amount_column
                            else None
                        ),
                        observed_at=self.calendar.session_close_at(trade_date),
                        source="akshare_eastmoney_daily",
                        fetched_at=fetched_at,
                    )
                )
        except Exception as exc:
            raise ProviderUnavailableError(
                f"daily turnover is unavailable: {type(exc).__name__}"
            ) from exc
        if not rows:
            raise ProviderUnavailableError("daily turnover returned no completed sessions")
        rows.sort(key=lambda item: item.trade_date)
        return rows

    def _completed_spot_frame(self, day: date):
        fetched_at = self._aware_now()
        if day != self.calendar.latest_completed_session(fetched_at):
            raise ProviderUnavailableError(
                "spot market aggregates only support the latest completed session"
            )
        if self.calendar.is_realtime_session(fetched_at):
            raise ProviderUnavailableError(
                "incomplete current-session aggregates are not formal daily data"
            )
        frame = self._retry("eastmoney-a-share-spot", self._ak().stock_zh_a_spot_em)
        return frame, fetched_at

    def get_market_breadth(self, day: date) -> list[MarketBreadthDaily]:
        frame, fetched_at = self._completed_spot_frame(day)
        change_column = self._column(frame, "\u6da8\u8dcc\u5e45", "change_pct")
        values = [
            value
            for raw in frame[change_column].tolist()
            if (value := self._optional_decimal(raw)) is not None
        ]
        if not values:
            raise ProviderUnavailableError("market breadth has no valid change values")
        ordered = sorted(values)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / Decimal("2")
        )
        return [
            MarketBreadthDaily(
                trade_date=day,
                advancing=sum(value > 0 for value in values),
                declining=sum(value < 0 for value in values),
                unchanged=sum(value == 0 for value in values),
                limit_up=sum(value >= Decimal("9.8") for value in values),
                limit_down=sum(value <= Decimal("-9.8") for value in values),
                new_highs=None,
                new_lows=None,
                median_change_pct=median,
                above_ma20_ratio=None,
                above_ma50_ratio=None,
                observed_at=self.calendar.session_close_at(day),
                source="akshare_eastmoney_a_spot",
                fetched_at=fetched_at,
            )
        ]

    def get_market_amount(self, day: date) -> list[MarketAmountDaily]:
        frame, fetched_at = self._completed_spot_frame(day)
        amount_column = self._column(frame, "\u6210\u4ea4\u989d", "amount")
        values = [
            value
            for raw in frame[amount_column].tolist()
            if (value := self._optional_decimal(raw)) is not None
        ]
        if not values:
            raise ProviderUnavailableError("market amount has no valid values")
        return [
            MarketAmountDaily(
                trade_date=day,
                total_amount=sum(values, Decimal("0")),
                observed_at=self.calendar.session_close_at(day),
                source="akshare_eastmoney_a_spot",
                fetched_at=fetched_at,
            )
        ]

    def get_market_amount_history(
        self, start: date, end: date
    ) -> list[MarketAmountDaily]:
        fetched_at = self._aware_now()
        series: list[dict[date, Decimal]] = []
        for symbol in ("sh000001", "sz399001"):
            try:
                frame = self._retry(
                    f"eastmoney-index-amount-{symbol}",
                    lambda symbol=symbol: self._ak().stock_zh_index_daily_em(symbol=symbol),
                )
                date_column = self._column(frame, "\u65e5\u671f", "date")
                amount_column = self._column(frame, "\u6210\u4ea4\u989d", "amount")
                values = {}
                for _, raw in frame.iterrows():
                    trade_date = date.fromisoformat(str(raw[date_column])[:10])
                    amount = self._optional_decimal(raw[amount_column])
                    if start <= trade_date <= end and amount is not None:
                        values[trade_date] = amount
                series.append(values)
            except Exception as exc:
                raise ProviderUnavailableError(
                    f"market amount history is unavailable: {type(exc).__name__}"
                ) from exc
        dates = sorted(set(series[0]) & set(series[1]))
        latest_completed = self.calendar.latest_completed_session(fetched_at)
        rows = [
            MarketAmountDaily(
                trade_date=trade_date,
                total_amount=series[0][trade_date] + series[1][trade_date],
                observed_at=self.calendar.session_close_at(trade_date),
                source="akshare_eastmoney_sh_sz_indices",
                fetched_at=fetched_at,
            )
            for trade_date in dates
            if trade_date <= latest_completed
        ]
        if not rows:
            raise ProviderUnavailableError("market amount history returned no completed rows")
        return rows

    def _benchmark_rows(self, frame, source: str) -> dict:
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
        raw_fetched_at = self.now_fn()
        return {
            "rows": rows,
            "source": source,
            "fetched_at": to_shanghai_aware(
                raw_fetched_at,
                naive_is_shanghai=raw_fetched_at.tzinfo is None,
            ),
        }

    def get_index_history(self, symbol: str, start: date, end: date) -> dict:
        eastmoney_symbol = (
            _AKSHARE_CSI300_EASTMONEY_SYMBOL
            if symbol == CSI300_INTERNAL_SYMBOL
            else symbol
        )
        fallback_symbol = (
            _AKSHARE_CSI300_FALLBACK_SYMBOL
            if symbol == CSI300_INTERNAL_SYMBOL
            else symbol[-6:]
        )
        errors = []
        providers = (
            (
                "东方财富指数",
                lambda: self._ak().stock_zh_index_daily_em(
                    symbol=eastmoney_symbol,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
                f"AKShare/东方财富指数 {eastmoney_symbol}",
            ),
            (
                "东方财富指数备用",
                lambda: self._ak().index_zh_a_hist(
                    symbol=fallback_symbol,
                    period="daily",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                ),
                f"AKShare/index_zh_a_hist {fallback_symbol}",
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

    def get_industry_daily(
        self, industry: str, start: date, end: date
    ) -> list[IndustryDaily]:
        fetched_at = self._aware_now()
        try:
            frame = self._retry(
                "eastmoney-industry-daily",
                lambda: self._ak().stock_board_industry_hist_em(
                    symbol=industry,
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    period="daily",
                    adjust="",
                ),
            )
            date_column = self._column(frame, "\u65e5\u671f", "date")
            change_column = next(
                (name for name in ("\u6da8\u8dcc\u5e45", "change_pct") if name in frame.columns),
                None,
            )
            amount_column = next(
                (name for name in ("\u6210\u4ea4\u989d", "amount") if name in frame.columns),
                None,
            )
            latest_completed = self.calendar.latest_completed_session(fetched_at)
            rows = []
            for _, raw in frame.iterrows():
                trade_date = date.fromisoformat(str(raw[date_column])[:10])
                if trade_date > latest_completed:
                    continue
                rows.append(
                    IndustryDaily(
                        industry=industry,
                        trade_date=trade_date,
                        change_pct=(
                            self._optional_decimal(raw[change_column])
                            if change_column
                            else None
                        ),
                        amount=(
                            self._optional_decimal(raw[amount_column])
                            if amount_column
                            else None
                        ),
                        amount_share=None,
                        advance_ratio=None,
                        limit_up_count=None,
                        leader_strength=None,
                        new_high_ratio=None,
                        observed_at=self.calendar.session_close_at(trade_date),
                        source="akshare_eastmoney_industry",
                        fetched_at=fetched_at,
                    )
                )
        except Exception as exc:
            raise ProviderUnavailableError(
                f"industry daily data is unavailable: {type(exc).__name__}"
            ) from exc
        if not rows:
            raise ProviderUnavailableError("industry daily data returned no rows")
        rows.sort(key=lambda item: item.trade_date)
        if rows[-1].trade_date != latest_completed:
            raise ProviderUnavailableError(
                "industry daily data does not include the latest completed session"
            )
        return rows

    def get_industry_constituents(
        self, industry: str
    ) -> list[IndustryConstituent]:
        fetched_at = self._aware_now()
        try:
            frame = self._retry(
                "eastmoney-industry-members",
                lambda: self._ak().stock_board_industry_cons_em(symbol=industry),
            )
            symbol_column = self._column(frame, "\u4ee3\u7801", "symbol", "code")
            name_column = self._column(frame, "\u540d\u79f0", "name")
            weight_column = next(
                (name for name in ("\u6743\u91cd", "weight") if name in frame.columns),
                None,
            )
            change_column = next(
                (name for name in ("\u6da8\u8dcc\u5e45", "change_pct") if name in frame.columns),
                None,
            )
            price_column = next(
                (name for name in ("\u6700\u65b0\u4ef7", "latest_price") if name in frame.columns),
                None,
            )
            high_column = next(
                (
                    name
                    for name in ("52\u5468\u6700\u9ad8", "\u6700\u9ad8", "high_52w")
                    if name in frame.columns
                ),
                None,
            )
            rows = [
                IndustryConstituent(
                    industry=industry,
                    symbol=str(raw[symbol_column]).zfill(6),
                    name=str(raw[name_column]),
                    weight=(
                        self._optional_decimal(raw[weight_column])
                        if weight_column
                        else None
                    ),
                    observed_at=fetched_at,
                    source="akshare_eastmoney_industry_members",
                    fetched_at=fetched_at,
                    change_pct=(
                        self._optional_decimal(raw[change_column])
                        if change_column
                        else None
                    ),
                    latest_price=(
                        self._optional_decimal(raw[price_column])
                        if price_column
                        else None
                    ),
                    high_52w=(
                        self._optional_decimal(raw[high_column])
                        if high_column
                        else None
                    ),
                    is_new_high=(
                        self._optional_decimal(raw[price_column])
                        >= self._optional_decimal(raw[high_column])
                        if price_column
                        and high_column
                        and self._optional_decimal(raw[price_column]) is not None
                        and self._optional_decimal(raw[high_column]) is not None
                        else None
                    ),
                )
                for _, raw in frame.iterrows()
            ]
        except Exception as exc:
            raise ProviderUnavailableError(
                f"industry constituents are unavailable: {type(exc).__name__}"
            ) from exc
        if not rows:
            raise ProviderUnavailableError("industry constituents returned no rows")
        return rows

    def list_industries(self) -> list[str]:
        try:
            frame = self._retry(
                "eastmoney-industry-list",
                self._ak().stock_board_industry_name_em,
            )
            name_column = self._column(
                frame, "\u677f\u5757\u540d\u79f0", "\u540d\u79f0", "name", "industry"
            )
            names = sorted(
                {
                    str(value).strip()
                    for value in frame[name_column].tolist()
                    if str(value).strip()
                }
            )
        except Exception as exc:
            raise ProviderUnavailableError(
                f"industry universe is unavailable: {type(exc).__name__}"
            ) from exc
        if not names:
            raise ProviderUnavailableError("industry universe returned no industries")
        return names

    def get_industry_constituents_universe(self) -> list[IndustryConstituent]:
        rows = [
            member
            for industry in self.list_industries()
            for member in self.get_industry_constituents(industry)
        ]
        if not rows:
            raise ProviderUnavailableError("industry constituent universe is empty")
        return rows

    def get_industry_universe(
        self, start: date, end: date
    ) -> list[IndustryDaily]:
        series = {
            industry: self.get_industry_daily(industry, start, end)
            for industry in self.list_industries()
        }
        constituents = {
            industry: self.get_industry_constituents(industry)
            for industry in series
        }
        totals: dict[date, Decimal] = {}
        for rows in series.values():
            for row in rows:
                if row.amount is not None:
                    totals[row.trade_date] = totals.get(row.trade_date, Decimal("0")) + row.amount
        result = []
        for industry, rows in sorted(series.items()):
            members = constituents[industry]
            changes = [item.change_pct for item in members if item.change_pct is not None]
            new_highs = [item.is_new_high for item in members if item.is_new_high is not None]
            latest_date = rows[-1].trade_date
            for row in rows:
                total = totals.get(row.trade_date)
                latest = row.trade_date == latest_date
                result.append(
                    replace(
                        row,
                        amount_share=(
                            row.amount / total
                            if row.amount is not None and total
                            else None
                        ),
                        advance_ratio=(
                            Decimal(sum(value > 0 for value in changes))
                            / Decimal(len(changes))
                            if latest and changes
                            else None
                        ),
                        limit_up_count=(
                            sum(value >= Decimal("9.8") for value in changes)
                            if latest and changes
                            else None
                        ),
                        leader_strength=max(changes) if latest and changes else None,
                        new_high_ratio=(
                            Decimal(sum(value is True for value in new_highs))
                            / Decimal(len(new_highs))
                            if latest and new_highs
                            else None
                        ),
                    )
                )
        if not result:
            raise ProviderUnavailableError("industry market universe is empty")
        return result

    def company_concepts(self, symbol: str) -> list[dict]:
        payload = self.company_industry_concepts(symbol)
        concepts = payload.get("concepts") or []
        fetched_at = self._aware_now()
        return [
            {
                "concept": item["name"],
                "relevance": item.get("relevance", "INSUFFICIENT_EVIDENCE"),
                "evidence_summary": item.get("evidence"),
                "source": payload.get("source", "AKShare"),
                "source_url": "https://webapi.cninfo.com.cn/",
                "observed_at": fetched_at,
                "fetched_at": fetched_at,
            }
            for item in concepts
            if item.get("name")
        ]

    def company_industry_chain(self, symbol: str) -> list[dict]:
        profile = self.company_profile(symbol)
        text = self._profile_evidence_text(profile)
        fetched_at = self._aware_now()
        mappings = (
            (("\u96c6\u6210\u7535\u8def\u8bbe\u8ba1", "\u82af\u7247\u8bbe\u8ba1"), "\u534a\u5bfc\u4f53\u4ea7\u4e1a\u94fe", "\u82af\u7247\u8bbe\u8ba1", "DESIGN"),
            (("\u6676\u5706\u5236\u9020",), "\u534a\u5bfc\u4f53\u4ea7\u4e1a\u94fe", "\u6676\u5706\u5236\u9020", "MANUFACTURING"),
            (("\u5c01\u88c5\u6d4b\u8bd5", "\u82af\u7247\u5c01\u88c5"), "\u534a\u5bfc\u4f53\u4ea7\u4e1a\u94fe", "\u5c01\u88c5\u6d4b\u8bd5", "PACKAGING_TEST"),
            (("\u5149\u4f0f\u7ec4\u4ef6",), "\u5149\u4f0f\u4ea7\u4e1a\u94fe", "\u7ec4\u4ef6", "DOWNSTREAM"),
            (("\u9502\u7535\u6c60\u6b63\u6781\u6750\u6599", "\u6b63\u6781\u6750\u6599"), "\u9502\u7535\u4ea7\u4e1a\u94fe", "\u6b63\u6781\u6750\u6599", "UPSTREAM"),
        )
        matched = next(
            (item for item in mappings if any(keyword in text for keyword in item[0])),
            None,
        )
        if matched is None:
            chain_name, node_name, stage, relevance = (
                "UNRESOLVED",
                "UNRESOLVED",
                "UNKNOWN",
                "INSUFFICIENT_EVIDENCE",
            )
        else:
            _, chain_name, node_name, stage = matched
            relevance = "CORE_BUSINESS"
        return [
            {
                "chain_name": chain_name,
                "node_name": node_name,
                "stage": stage,
                "relevance": relevance,
                "primary_products": [],
                "revenue_relevance": "unknown",
                "source": "AKShare/CNINFO company profile",
                "source_url": "https://webapi.cninfo.com.cn/",
                "evidence_summary": text[:1000] or "company profile has no proving business text",
                "observed_at": fetched_at,
                "fetched_at": fetched_at,
            }
        ]

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

    def _legacy_company_industry_concepts(self, symbol: str) -> dict:
        profile = self.company_profile(symbol)
        industry = profile.get("细分行业") or profile.get("所属行业")
        return {
            "industries": [{"name": industry, "level": "provider"}] if industry else [],
            "concepts": [],
            "source": "AKShare/巨潮公司概况",
        }

    def company_industry_concepts(self, symbol: str) -> dict:
        profile = self.company_profile(symbol)
        industry = profile.get("\u7ec6\u5206\u884c\u4e1a") or profile.get("\u6240\u5c5e\u884c\u4e1a")
        text = self._profile_evidence_text(profile)
        catalog = (
            ("\u534a\u5bfc\u4f53", ("\u534a\u5bfc\u4f53", "\u82af\u7247", "\u96c6\u6210\u7535\u8def")),
            ("\u4eba\u5de5\u667a\u80fd", ("\u4eba\u5de5\u667a\u80fd", "AI\u670d\u52a1\u5668", "AI\u82af\u7247")),
            ("\u673a\u5668\u4eba", ("\u673a\u5668\u4eba", "\u673a\u5668\u89c6\u89c9")),
            ("\u5149\u4f0f", ("\u5149\u4f0f", "\u592a\u9633\u80fd\u7535\u6c60")),
            ("\u9502\u7535\u6c60", ("\u9502\u7535\u6c60", "\u6b63\u6781\u6750\u6599", "\u8d1f\u6781\u6750\u6599")),
        )
        concepts = [
            {"name": name, "evidence": text[:1000], "relevance": "CORE_BUSINESS"}
            for name, keywords in catalog
            if any(keyword.lower() in text.lower() for keyword in keywords)
        ]
        if industry and all(item["name"] != str(industry).strip() for item in concepts):
            concepts.append(
                {
                    "name": str(industry).strip(),
                    "evidence": f"CNINFO industry classification: {industry}",
                    "relevance": "IMPORTANT_BUSINESS",
                }
            )
        if not concepts:
            concepts.append(
                {
                    "name": "UNRESOLVED",
                    "evidence": text[:1000] or "company profile has no proving business text",
                    "relevance": "INSUFFICIENT_EVIDENCE",
                }
            )
        return {
            "industries": [{"name": industry, "level": "provider"}] if industry else [],
            "concepts": concepts,
            "source": "AKShare/CNINFO company profile",
        }

    @staticmethod
    def _profile_evidence_text(profile: dict) -> str:
        fields = (
            "\u4e3b\u8425\u4e1a\u52a1",
            "\u7ecf\u8425\u8303\u56f4",
            "\u516c\u53f8\u7b80\u4ecb",
            "\u516c\u53f8\u4e3b\u8981\u4ea7\u54c1",
            "\u7ec6\u5206\u884c\u4e1a",
            "\u6240\u5c5e\u884c\u4e1a",
        )
        return " | ".join(
            f"{field}: {str(profile[field]).strip()}"
            for field in fields
            if profile.get(field) is not None and str(profile[field]).strip()
        )

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
                    symbol=symbol,
                    start_date="19900101",
                    end_date=shanghai_today().strftime("%Y%m%d"),
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
