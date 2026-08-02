from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from app.domain.repository import (
    AccountRecord,
    MarketBarRecord,
    MarketQuoteRecord,
    RuleVersionRecord,
)
from app.schemas_workflow import TradePlanPreviewRequest
from app.services.trade_plan.application import generate_trade_plan
from app.services.trade_plan.compatibility import GENERATOR_PARAMETERS, GENERATOR_RULES


class FakeTradePlanRepository:
    def __init__(self) -> None:
        self.calls = []
        rows = []
        for index in range(260):
            close = 5 + index * 0.019
            rows.append((close, close + 0.08, close - 0.08, 100.0))
        for index in range(25):
            close = 10 + (index % 3 - 1) * 0.03
            rows.append((close, close + 0.12, close - 0.12, 100.0))
        rows.extend(
            [
                (10.55, 10.65, 10.15, 220.0),
                (10.32, 10.48, 10.12, 55.0),
                (10.82, 10.9, 10.3, 180.0),
            ]
        )
        start = date.today() - timedelta(days=len(rows) - 1)
        self.frame = pd.DataFrame(
            [
                {
                    "Date": start + timedelta(days=index),
                    "Open": close - 0.03,
                    "High": high,
                    "Low": low,
                    "Close": close,
                    "Volume": volume,
                }
                for index, (close, high, low, volume) in enumerate(rows)
            ]
        ).set_index(pd.to_datetime([start + timedelta(days=i) for i in range(len(rows))]))

    def _call(self, name):
        self.calls.append(name)

    def get_account(self, account_id):
        self._call("get_account")
        return AccountRecord(account_id, Decimal(100000), Decimal(80000))

    def get_holding(self, account_id, symbol):
        self._call("get_holding")

    def get_holdings(self, account_id):
        self._call("get_holdings")
        return ()

    def get_company_profile(self, symbol):
        self._call("get_company_profile")

    def get_latest_bar(self, symbol):
        self._call("get_latest_bar")
        return MarketBarRecord(
            symbol,
            date.today(),
            Decimal("10.82"),
            "fixture",
            datetime.now(),
        )

    def get_quote(self, symbol):
        self._call("get_quote")
        return MarketQuoteRecord(symbol, "测试公司", Decimal("10.82"))

    def load_qfq_frame(self, symbol):
        self._call("load_qfq_frame")
        return self.frame

    def ensure_rule_version(self):
        self._call("ensure_rule_version")
        return RuleVersionRecord("1.2.0", GENERATOR_PARAMETERS, GENERATOR_RULES)


def test_application_runs_complete_chain_with_repository_and_without_session():
    repository = FakeTradePlanRepository()
    request = TradePlanPreviewRequest(
        symbol="300502",
        account_id=1,
        market_state="上升",
        sector_state="强",
    )

    result = generate_trade_plan(None, request, repository=repository)

    assert result["pattern"]
    assert result["position_calculation"]
    assert result["preview_hash"]
    assert repository.calls == [
        "get_account",
        "ensure_rule_version",
        "get_company_profile",
        "get_holding",
        "get_latest_bar",
        "get_quote",
        "load_qfq_frame",
        "get_holdings",
    ]
