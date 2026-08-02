from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.domain.repository.models import (
    AccountRecord,
    CompanyProfileRecord,
    HoldingRecord,
    MarketBarRecord,
    MarketQuoteRecord,
    RuleVersionRecord,
)


class TradePlanRepository(Protocol):
    def get_account(self, account_id: int) -> AccountRecord | None: ...

    def get_holding(self, account_id: int, symbol: str) -> HoldingRecord | None: ...

    def get_holdings(self, account_id: int) -> tuple[HoldingRecord, ...]: ...

    def get_company_profile(self, symbol: str) -> CompanyProfileRecord | None: ...

    def get_latest_bar(self, symbol: str) -> MarketBarRecord | None: ...

    def get_quote(self, symbol: str) -> MarketQuoteRecord | None: ...

    def load_qfq_frame(self, symbol: str) -> pd.DataFrame: ...

    def ensure_rule_version(self) -> RuleVersionRecord: ...


TradePlanReadRepository = TradePlanRepository
