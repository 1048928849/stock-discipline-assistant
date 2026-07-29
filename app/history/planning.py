from __future__ import annotations

from datetime import date, timedelta

from app.data_hub.trading_calendar import TradingCalendar
from app.discovery.contracts import IndustryDiscoveryInput
from app.discovery.scoring import IndustryDiscoveryScorer
from app.history.contracts import HistoryRequirementPlan
from app.domain.market_symbols import CSI300_INTERNAL_SYMBOL


class HistoryRequirementPlanner:
    def __init__(
        self,
        *,
        scorer: IndustryDiscoveryScorer,
        minimum_rows: int,
        lookback_sessions: int,
        rewrite_sessions: int,
        adapter_version: str,
    ) -> None:
        self.scorer = scorer
        self.minimum_rows = minimum_rows
        self.lookback_sessions = lookback_sessions
        self.rewrite_sessions = rewrite_sessions
        self.adapter_version = adapter_version

    @staticmethod
    def _start_date(
        trade_date: date, sessions: int, calendar: TradingCalendar
    ) -> date:
        selected = []
        current = trade_date
        is_session = getattr(calendar, "is_session", None)
        if not callable(is_session):
            raise TypeError("history planning calendar must expose is_session")
        while len(selected) < sessions:
            if is_session(current):
                selected.append(current)
            current -= timedelta(days=1)
        return selected[-1]

    def build(
        self,
        *,
        trade_date: date,
        industries: tuple[IndustryDiscoveryInput, ...],
        constituents: dict[str, tuple[str, ...]],
        calendar: TradingCalendar,
    ) -> HistoryRequirementPlan:
        ranked = self.scorer.rank(industries)
        selected = tuple(
            item.industry_key
            for item, _score in ranked[: self.scorer.config.max_industries]
        )
        symbols = tuple(
            sorted(
                {
                    symbol
                    for industry_key in selected
                    for symbol in constituents.get(industry_key, ())
                }
            )
        )
        sessions = max(
            self.minimum_rows,
            self.lookback_sessions,
            self.rewrite_sessions,
        )
        return HistoryRequirementPlan.create(
            trade_date=trade_date,
            benchmark_symbols=(CSI300_INTERNAL_SYMBOL,),
            selected_industries=selected,
            required_stock_symbols=symbols,
            required_capabilities=(
                "market.index_daily",
                "market.daily.qfq",
                "market.turnover.daily",
            ),
            start_date=self._start_date(trade_date, sessions, calendar),
            end_date=trade_date,
            minimum_rows=self.minimum_rows,
            config={
                "discovery_config_hash": self.scorer.config.config_hash(),
                "lookback_sessions": self.lookback_sessions,
                "rewrite_sessions": self.rewrite_sessions,
                "adapter_version": self.adapter_version,
            },
        )


__all__ = ["HistoryRequirementPlanner"]
