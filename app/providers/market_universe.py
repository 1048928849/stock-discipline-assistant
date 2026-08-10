from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime
from typing import Any

from app.config import Settings
from app.data_hub.contracts import ProviderUnavailableError
from app.data_hub.trading_calendar import TradingCalendar, get_trading_calendar, to_shanghai_aware
from app.market_breadth.reconciliation import (
    CANONICAL_A_SHARE_SH_SZ_V1,
    Board,
    Exchange,
    MarketUniverseMembership,
    SecurityType,
    TradableStatus,
    classify_symbol,
)
from app.providers.market_universe_worker import MAX_OUTPUT_BYTES, OPERATION, PROTOCOL_VERSION


class UniverseMembershipUnavailableError(ProviderUnavailableError):
    pass


class BaoStockSecurityMasterProvider:
    source = "baostock-security-master"

    def __init__(
        self,
        settings: Settings,
        *,
        calendar: TradingCalendar | None = None,
        runner=None,
    ) -> None:
        self.settings = settings
        self.calendar = calendar or get_trading_calendar()
        self.runner = runner or self._run

    def _run(self, trade_date: date) -> dict[str, Any]:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.providers.market_universe_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        payload = json.dumps(
            {"operation": OPERATION, "trade_date": trade_date.isoformat()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            stdout, stderr = process.communicate(
                payload, timeout=self.settings.market_breadth_worker_timeout_seconds
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise UniverseMembershipUnavailableError(
                "UNIVERSE_MEMBERSHIP_UNAVAILABLE: BaoStock worker timed out"
            ) from exc
        if process.poll() is None:
            process.wait()
        if len(stdout) > MAX_OUTPUT_BYTES:
            raise UniverseMembershipUnavailableError(
                "UNIVERSE_MEMBERSHIP_UNAVAILABLE: response exceeds limit"
            )
        if process.returncode != 0:
            detail = stderr.decode(errors="replace")[:300]
            try:
                worker_error = json.loads(stdout)
                detail = str(worker_error.get("message", detail))[:300]
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                pass
            raise UniverseMembershipUnavailableError("UNIVERSE_MEMBERSHIP_UNAVAILABLE: " + detail)
        try:
            response = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UniverseMembershipUnavailableError(
                "UNIVERSE_MEMBERSHIP_UNAVAILABLE: invalid worker JSON"
            ) from exc
        return response

    def get_membership(self, trade_date: date) -> list[MarketUniverseMembership]:
        response = self.runner(trade_date)
        expected = {
            "protocol_version",
            "operation",
            "trade_date",
            "source",
            "fields",
            "rows",
            "row_count",
            "response_digest",
            "fetched_at",
        }
        if (
            not isinstance(response, dict)
            or set(response) != expected
            or response["protocol_version"] != PROTOCOL_VERSION
            or response["operation"] != OPERATION
            or response["trade_date"] != trade_date.isoformat()
            or response["row_count"] != len(response["rows"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(response["response_digest"]))
        ):
            raise UniverseMembershipUnavailableError(
                "UNIVERSE_MEMBERSHIP_UNAVAILABLE: worker schema mismatch"
            )
        fetched_at = to_shanghai_aware(datetime.fromisoformat(response["fetched_at"]))
        result = []
        for raw in response["rows"]:
            if set(raw) != {"code", "code_name", "ipoDate", "outDate", "type", "status"}:
                raise UniverseMembershipUnavailableError(
                    "UNIVERSE_MEMBERSHIP_UNAVAILABLE: row schema mismatch"
                )
            external = str(raw["code"]).lower()
            if str(raw["type"]) != "1":
                continue
            match = re.fullmatch(r"(sh|sz)\.(\d{6})", external)
            if not match:
                continue
            symbol = match.group(2)
            exchange, board = classify_symbol(symbol)
            if exchange not in {Exchange.SH, Exchange.SZ} or board == Board.UNKNOWN:
                continue
            security_type = SecurityType.COMMON_STOCK
            try:
                listing_date = date.fromisoformat(raw["ipoDate"])
                delisting_date = date.fromisoformat(raw["outDate"]) if raw["outDate"] else None
            except ValueError as exc:
                raise UniverseMembershipUnavailableError(
                    "UNIVERSE_MEMBERSHIP_UNAVAILABLE: invalid listing dates"
                ) from exc
            result.append(
                MarketUniverseMembership.build(
                    spec=CANONICAL_A_SHARE_SH_SZ_V1,
                    trade_date=trade_date,
                    symbol=symbol,
                    exchange=exchange,
                    board=board,
                    security_type=security_type,
                    tradable_status=(
                        TradableStatus.ACTIVE if raw["status"] == "1" else TradableStatus.UNKNOWN
                    ),
                    listing_date=listing_date,
                    delisting_date=delisting_date,
                    source=self.source,
                    source_reference=f"{response['response_digest']}:{external}",
                    observed_at=fetched_at,
                )
            )
        if not result:
            raise UniverseMembershipUnavailableError(
                "UNIVERSE_MEMBERSHIP_UNAVAILABLE: no valid SH/SZ members"
            )
        return result
