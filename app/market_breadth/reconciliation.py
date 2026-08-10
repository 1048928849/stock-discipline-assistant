from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable


_SYMBOL = re.compile(r"^\d{6}$")


class Exchange(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BSE = "BSE"
    UNKNOWN = "UNKNOWN"


class Board(StrEnum):
    SH_MAIN = "SH_MAIN"
    SH_STAR = "SH_STAR"
    SZ_MAIN = "SZ_MAIN"
    SZ_CHINEXT = "SZ_CHINEXT"
    BSE = "BSE"
    UNKNOWN = "UNKNOWN"


class SecurityType(StrEnum):
    COMMON_STOCK = "COMMON_STOCK"
    PREFERRED_OR_SPECIAL = "PREFERRED_OR_SPECIAL"
    ETF = "ETF"
    FUND = "FUND"
    BOND = "BOND"
    INDEX = "INDEX"
    OTHER = "OTHER"


class MembershipQuality(StrEnum):
    VERIFIED = "VERIFIED"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICTED = "CONFLICTED"


class ReconciliationClass(StrEnum):
    PRESENT_PRIMARY = "PRESENT_PRIMARY"
    PRESENT_SUPPLEMENTARY = "PRESENT_SUPPLEMENTARY"
    NOT_LISTED_ON_DATE = "NOT_LISTED_ON_DATE"
    NOT_IN_UNIVERSE = "NOT_IN_UNIVERSE"
    SUSPENDED_NO_BAR = "SUSPENDED_NO_BAR"
    PROVIDER_MISSING = "PROVIDER_MISSING"
    INVALID_SECURITY = "INVALID_SECURITY"
    UNRESOLVED = "UNRESOLVED"


class PoolReconciliationClass(StrEnum):
    IN_CANONICAL_UNIVERSE = "IN_CANONICAL_UNIVERSE"
    OUTSIDE_UNIVERSE = "OUTSIDE_UNIVERSE"
    MISSING_FROM_MEMBERSHIP = "MISSING_FROM_MEMBERSHIP"
    MISSING_FROM_CROSS_SECTION = "MISSING_FROM_CROSS_SECTION"
    INVALID_FOR_DATE = "INVALID_FOR_DATE"


class ReconciliationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    COMPLETE_WITH_SUPPLEMENTARY_DATA = "COMPLETE_WITH_SUPPLEMENTARY_DATA"
    INCOMPLETE = "INCOMPLETE"
    UNIVERSE_UNAVAILABLE = "UNIVERSE_UNAVAILABLE"


class TradableStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MarketBreadthUniverseSpec:
    universe_id: str
    version: str
    market: str
    included_exchanges: tuple[Exchange, ...]
    included_boards: tuple[Board, ...]
    security_types: tuple[SecurityType, ...]
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    effective_from: date
    effective_to: date | None
    membership_provider: str
    membership_quality: MembershipQuality
    price_provider_policy: str
    limit_pool_provider_policy: str
    non_trading_member_policy: str

    def contains(self, member: MarketUniverseMembership) -> bool:
        return (
            member.exchange in self.included_exchanges
            and member.board in self.included_boards
            and member.security_type in self.security_types
        )


CANONICAL_A_SHARE_SH_SZ_V1 = MarketBreadthUniverseSpec(
    universe_id="A_SHARE_SH_SZ",
    version="1.0.0",
    market="CN",
    included_exchanges=(Exchange.SH, Exchange.SZ),
    included_boards=(Board.SH_MAIN, Board.SH_STAR, Board.SZ_MAIN, Board.SZ_CHINEXT),
    security_types=(SecurityType.COMMON_STOCK,),
    inclusion_rules=(
        "security is a Shanghai or Shenzhen listed common A-share",
        "listing_date <= trade_date",
        "delisting_date is absent or trade_date < delisting_date",
    ),
    exclusion_rules=(
        "BSE securities",
        "B shares, ETFs, funds, bonds, indices and other non-common-stock instruments",
        "securities not yet listed or already delisted on the trade date",
    ),
    effective_from=date(2026, 7, 1),
    effective_to=None,
    membership_provider="baostock-security-master",
    membership_quality=MembershipQuality.SINGLE_SOURCE,
    price_provider_policy="FreeStockDB primary; bounded trusted exact-date public history supplement",
    limit_pool_provider_policy="AKShare raw pools projected onto the exact canonical universe",
    non_trading_member_policy="EXCLUDE_EVIDENCED_SUSPENDED_MEMBER",
)


def classify_symbol(symbol: str) -> tuple[Exchange, Board]:
    """Deterministic exchange code rules; never uses a display name."""
    if not _SYMBOL.fullmatch(symbol):
        return Exchange.UNKNOWN, Board.UNKNOWN
    if symbol.startswith(("600", "601", "603", "605")):
        return Exchange.SH, Board.SH_MAIN
    if symbol.startswith(("688", "689")):
        return Exchange.SH, Board.SH_STAR
    if symbol.startswith(("000", "001", "002", "003")):
        return Exchange.SZ, Board.SZ_MAIN
    if symbol.startswith(("300", "301")):
        return Exchange.SZ, Board.SZ_CHINEXT
    if symbol.startswith(("4", "8", "92")):
        return Exchange.BSE, Board.BSE
    return Exchange.UNKNOWN, Board.UNKNOWN


@dataclass(frozen=True)
class MarketUniverseMembership:
    universe_id: str
    universe_version: str
    trade_date: date
    symbol: str
    exchange: Exchange
    board: Board
    security_type: SecurityType
    listed_on_date: bool
    tradable_status: TradableStatus
    listing_date: date
    delisting_date: date | None
    source: str
    source_reference: str
    observed_at: datetime
    digest: str

    @classmethod
    def build(
        cls,
        *,
        spec: MarketBreadthUniverseSpec,
        trade_date: date,
        symbol: str,
        exchange: Exchange,
        board: Board,
        security_type: SecurityType,
        tradable_status: TradableStatus,
        listing_date: date,
        delisting_date: date | None,
        source: str,
        source_reference: str,
        observed_at: datetime,
    ) -> MarketUniverseMembership:
        listed = listing_date <= trade_date and (
            delisting_date is None or trade_date < delisting_date
        )
        business = {
            "universe_id": spec.universe_id,
            "universe_version": spec.version,
            "trade_date": trade_date.isoformat(),
            "symbol": symbol,
            "exchange": exchange.value,
            "board": board.value,
            "security_type": security_type.value,
            "listed_on_date": listed,
            "tradable_status": tradable_status.value,
            "listing_date": listing_date.isoformat(),
            "delisting_date": delisting_date.isoformat() if delisting_date else None,
            "source": source,
            "source_reference": source_reference,
        }
        return cls(
            universe_id=spec.universe_id,
            universe_version=spec.version,
            trade_date=trade_date,
            symbol=symbol,
            exchange=exchange,
            board=board,
            security_type=security_type,
            listed_on_date=listed,
            tradable_status=tradable_status,
            listing_date=listing_date,
            delisting_date=delisting_date,
            source=source,
            source_reference=source_reference,
            observed_at=observed_at,
            digest=_digest(business),
        )


# The canonical membership is derived for each trade date; this public name makes
# the snapshot semantics explicit without requiring a second mutable representation.
MarketUniverseMembershipSnapshot = MarketUniverseMembership


@dataclass(frozen=True)
class BreadthPriceEvidence:
    symbol: str
    trade_date: date
    close: Decimal
    previous_close: Decimal
    source: str
    source_reference: str
    price_unit: str = "CNY"
    adjustment: str = "unadjusted_or_daily_return_compatible"


@dataclass(frozen=True)
class MemberReconciliation:
    symbol: str
    classification: ReconciliationClass
    source: str | None
    reason: str | None = None


@dataclass(frozen=True)
class PoolSymbolReconciliation:
    symbol: str
    classification: PoolReconciliationClass


@dataclass(frozen=True)
class BreadthUniverseReconciliationReport:
    universe_spec: MarketBreadthUniverseSpec
    trade_date: date
    expected_member_count: int
    active_trading_member_count: int
    primary_rows: int
    supplementary_rows: int
    excluded_legitimate_members: int
    unresolved_missing_members: tuple[str, ...]
    limit_up_raw_count: int
    limit_up_in_scope_count: int
    limit_down_raw_count: int
    limit_down_in_scope_count: int
    pool_symbols_outside_scope: tuple[str, ...]
    membership_conflicts: tuple[str, ...]
    completeness_status: ReconciliationStatus
    degradation_reason_codes: tuple[str, ...]
    members: tuple[MemberReconciliation, ...]
    limit_up_members: tuple[str, ...]
    limit_down_members: tuple[str, ...]
    limit_up_reconciliation: tuple[PoolSymbolReconciliation, ...]
    limit_down_reconciliation: tuple[PoolSymbolReconciliation, ...]
    membership_digest: str
    reconciliation_digest: str
    business_identity: str
    price_evidence: tuple[BreadthPriceEvidence, ...]

    @property
    def persistable(self) -> bool:
        return self.completeness_status in {
            ReconciliationStatus.COMPLETE,
            ReconciliationStatus.COMPLETE_WITH_SUPPLEMENTARY_DATA,
        }

    def as_persisted_metadata(self) -> dict[str, Any]:
        return {
            "universe_id": self.universe_spec.universe_id,
            "universe_version": self.universe_spec.version,
            "membership_provider": self.universe_spec.membership_provider,
            "membership_digest": self.membership_digest,
            "reconciliation_digest": self.reconciliation_digest,
            "business_identity": self.business_identity,
            "reconciliation_status": self.completeness_status.value,
            "expected_member_count": self.expected_member_count,
            "active_trading_member_count": self.active_trading_member_count,
            "primary_count": self.primary_rows,
            "supplementary_count": self.supplementary_rows,
            "excluded_legitimate_members": self.excluded_legitimate_members,
            "unresolved_missing_members": list(self.unresolved_missing_members),
            "degradation_reason_codes": list(self.degradation_reason_codes),
            "pool_symbols_outside_scope": list(self.pool_symbols_outside_scope),
        }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _unique_members(
    members: Iterable[MarketUniverseMembership],
) -> tuple[dict[str, MarketUniverseMembership], list[str]]:
    result: dict[str, MarketUniverseMembership] = {}
    conflicts: list[str] = []
    for member in members:
        if member.symbol in result:
            conflicts.append(member.symbol)
        else:
            result[member.symbol] = member
    return result, sorted(set(conflicts))


def _unique_prices(
    evidence: Iterable[BreadthPriceEvidence], *, label: str
) -> tuple[dict[str, BreadthPriceEvidence], list[str]]:
    result: dict[str, BreadthPriceEvidence] = {}
    conflicts: list[str] = []
    for item in evidence:
        if item.symbol in result:
            conflicts.append(f"{label}:{item.symbol}")
        else:
            result[item.symbol] = item
    return result, conflicts


def reconcile_market_breadth_universe(
    *,
    spec: MarketBreadthUniverseSpec,
    trade_date: date,
    membership: Iterable[MarketUniverseMembership] | None,
    primary: Iterable[BreadthPriceEvidence],
    supplementary: Iterable[BreadthPriceEvidence] = (),
    raw_limit_up: Iterable[str] = (),
    raw_limit_down: Iterable[str] = (),
) -> BreadthUniverseReconciliationReport:
    raw_limit_up = tuple(raw_limit_up)
    raw_limit_down = tuple(raw_limit_down)
    if membership is None:
        return _unavailable_report(spec, trade_date)
    member_map, conflicts = _unique_members(membership)
    primary_map, primary_conflicts = _unique_prices(primary, label="primary")
    supplementary_map, supplementary_conflicts = _unique_prices(
        supplementary, label="supplementary"
    )
    conflicts.extend(primary_conflicts + supplementary_conflicts)
    if any(
        member.trade_date != trade_date
        or member.universe_id != spec.universe_id
        or member.universe_version != spec.version
        for member in member_map.values()
    ):
        conflicts.append("membership_scope")

    expected: dict[str, MarketUniverseMembership] = {}
    reconciled: list[MemberReconciliation] = []
    for symbol, member in sorted(member_map.items()):
        if not member.listed_on_date:
            reconciled.append(
                MemberReconciliation(symbol, ReconciliationClass.NOT_LISTED_ON_DATE, None)
            )
        elif member.board == Board.UNKNOWN or member.exchange == Exchange.UNKNOWN:
            conflicts.append(symbol)
            reconciled.append(
                MemberReconciliation(
                    symbol, ReconciliationClass.INVALID_SECURITY, None, "UNKNOWN_BOARD"
                )
            )
        elif not spec.contains(member):
            reconciled.append(
                MemberReconciliation(symbol, ReconciliationClass.NOT_IN_UNIVERSE, None)
            )
        else:
            expected[symbol] = member

    used: list[BreadthPriceEvidence] = []
    unresolved: list[str] = []
    excluded_legitimate = 0
    supplementary_count = 0
    for symbol, member in sorted(expected.items()):
        primary_row = primary_map.get(symbol)
        supplementary_row = supplementary_map.get(symbol)
        if primary_row is not None and _valid_price(primary_row, symbol, trade_date):
            used.append(primary_row)
            reconciled.append(
                MemberReconciliation(
                    symbol, ReconciliationClass.PRESENT_PRIMARY, primary_row.source
                )
            )
        elif supplementary_row is not None and _valid_price(supplementary_row, symbol, trade_date):
            used.append(supplementary_row)
            supplementary_count += 1
            reconciled.append(
                MemberReconciliation(
                    symbol,
                    ReconciliationClass.PRESENT_SUPPLEMENTARY,
                    supplementary_row.source,
                    "BREADTH_PRIMARY_PROVIDER_GAP_SUPPLEMENTED",
                )
            )
        elif member.tradable_status == TradableStatus.SUSPENDED:
            excluded_legitimate += 1
            reconciled.append(
                MemberReconciliation(
                    symbol,
                    ReconciliationClass.SUSPENDED_NO_BAR,
                    member.source,
                    "EXCLUDE_EVIDENCED_SUSPENDED_MEMBER",
                )
            )
        else:
            unresolved.append(symbol)
            reconciled.append(
                MemberReconciliation(
                    symbol,
                    ReconciliationClass.PROVIDER_MISSING,
                    None,
                    "NO_EXACT_DATE_PRICE_EVIDENCE",
                )
            )

    up, up_reconciliation, up_outside, up_missing = _reconcile_pool(
        raw_limit_up, member_map, expected, {row.symbol for row in used}, trade_date, spec
    )
    down, down_reconciliation, down_outside, down_missing = _reconcile_pool(
        raw_limit_down, member_map, expected, {row.symbol for row in used}, trade_date, spec
    )
    unresolved.extend(up_missing + down_missing)
    unresolved = sorted(set(unresolved))
    conflicts = sorted(set(conflicts))
    degradation: list[str] = []
    if supplementary_count:
        degradation.append("BREADTH_PRIMARY_PROVIDER_GAP_SUPPLEMENTED")
    if unresolved:
        degradation.append("BREADTH_UNIVERSE_INCOMPLETE")
    if conflicts:
        degradation.append("BREADTH_MEMBERSHIP_CONFLICT")
    if conflicts or unresolved:
        status = ReconciliationStatus.INCOMPLETE
    elif supplementary_count:
        status = ReconciliationStatus.COMPLETE_WITH_SUPPLEMENTARY_DATA
    else:
        status = ReconciliationStatus.COMPLETE

    membership_business = [[symbol, member.digest] for symbol, member in sorted(member_map.items())]
    membership_digest = _digest(membership_business)
    reconciliation_business = {
        "universe_id": spec.universe_id,
        "universe_version": spec.version,
        "trade_date": trade_date.isoformat(),
        "members": [
            [item.symbol, item.classification.value, item.source, item.reason]
            for item in sorted(reconciled, key=lambda value: value.symbol)
        ],
        "prices": [
            [
                item.symbol,
                format(item.close, "f"),
                format(item.previous_close, "f"),
                item.source,
            ]
            for item in sorted(used, key=lambda value: value.symbol)
        ],
        "limit_up": sorted(up),
        "limit_down": sorted(down),
    }
    reconciliation_digest = _digest(reconciliation_business)
    identity = _digest(
        {
            "universe_id": spec.universe_id,
            "universe_version": spec.version,
            "trade_date": trade_date.isoformat(),
            "membership_digest": membership_digest,
            "reconciliation_digest": reconciliation_digest,
        }
    )
    return BreadthUniverseReconciliationReport(
        universe_spec=spec,
        trade_date=trade_date,
        expected_member_count=len(expected),
        active_trading_member_count=len(used),
        primary_rows=sum(item.symbol in primary_map for item in used),
        supplementary_rows=supplementary_count,
        excluded_legitimate_members=excluded_legitimate,
        unresolved_missing_members=tuple(unresolved),
        limit_up_raw_count=len(raw_limit_up),
        limit_up_in_scope_count=len(up),
        limit_down_raw_count=len(raw_limit_down),
        limit_down_in_scope_count=len(down),
        pool_symbols_outside_scope=tuple(sorted(set(up_outside + down_outside))),
        membership_conflicts=tuple(conflicts),
        completeness_status=status,
        degradation_reason_codes=tuple(degradation),
        members=tuple(sorted(reconciled, key=lambda value: value.symbol)),
        limit_up_members=tuple(sorted(up)),
        limit_down_members=tuple(sorted(down)),
        limit_up_reconciliation=tuple(up_reconciliation),
        limit_down_reconciliation=tuple(down_reconciliation),
        membership_digest=membership_digest,
        reconciliation_digest=reconciliation_digest,
        business_identity=identity,
        price_evidence=tuple(sorted(used, key=lambda value: value.symbol)),
    )


def _valid_price(item: BreadthPriceEvidence, symbol: str, trade_date: date) -> bool:
    return (
        item.symbol == symbol
        and item.trade_date == trade_date
        and item.close.is_finite()
        and item.previous_close.is_finite()
        and item.close > 0
        and item.previous_close > 0
        and item.price_unit == "CNY"
        and item.adjustment in {"unadjusted_or_daily_return_compatible", "qfq"}
    )


def _reconcile_pool(
    raw: Iterable[str],
    member_map: dict[str, MarketUniverseMembership],
    expected: dict[str, MarketUniverseMembership],
    prices: set[str],
    trade_date: date,
    spec: MarketBreadthUniverseSpec,
) -> tuple[set[str], list[PoolSymbolReconciliation], list[str], list[str]]:
    raw_values = tuple(raw)
    if len(set(raw_values)) != len(raw_values):
        return (
            set(),
            [PoolSymbolReconciliation("<duplicate>", PoolReconciliationClass.INVALID_FOR_DATE)],
            [],
            ["<duplicate_pool_symbol>"],
        )
    accepted: set[str] = set()
    result: list[PoolSymbolReconciliation] = []
    outside: list[str] = []
    missing: list[str] = []
    for symbol in sorted(raw_values):
        member = member_map.get(symbol)
        exchange, board = classify_symbol(symbol)
        if member is None and symbol.startswith(("200", "900", "5", "15", "16", "18", "11", "12")):
            classification = PoolReconciliationClass.OUTSIDE_UNIVERSE
            outside.append(symbol)
        elif (
            member is None
            and exchange == Exchange.BSE
            and board == Board.BSE
            and Exchange.BSE not in spec.included_exchanges
        ):
            classification = PoolReconciliationClass.OUTSIDE_UNIVERSE
            outside.append(symbol)
        elif member is None:
            classification = PoolReconciliationClass.MISSING_FROM_MEMBERSHIP
            missing.append(symbol)
        elif symbol not in expected:
            classification = PoolReconciliationClass.OUTSIDE_UNIVERSE
            outside.append(symbol)
        elif not member.listed_on_date or member.trade_date != trade_date:
            classification = PoolReconciliationClass.INVALID_FOR_DATE
            missing.append(symbol)
        elif symbol not in prices:
            classification = PoolReconciliationClass.MISSING_FROM_CROSS_SECTION
            missing.append(symbol)
        else:
            classification = PoolReconciliationClass.IN_CANONICAL_UNIVERSE
            accepted.add(symbol)
        result.append(PoolSymbolReconciliation(symbol, classification))
    return accepted, result, outside, missing


def _unavailable_report(
    spec: MarketBreadthUniverseSpec, trade_date: date
) -> BreadthUniverseReconciliationReport:
    digest = _digest(
        {
            "universe_id": spec.universe_id,
            "version": spec.version,
            "trade_date": trade_date.isoformat(),
            "status": ReconciliationStatus.UNIVERSE_UNAVAILABLE.value,
        }
    )
    return BreadthUniverseReconciliationReport(
        universe_spec=spec,
        trade_date=trade_date,
        expected_member_count=0,
        active_trading_member_count=0,
        primary_rows=0,
        supplementary_rows=0,
        excluded_legitimate_members=0,
        unresolved_missing_members=(),
        limit_up_raw_count=0,
        limit_up_in_scope_count=0,
        limit_down_raw_count=0,
        limit_down_in_scope_count=0,
        pool_symbols_outside_scope=(),
        membership_conflicts=(),
        completeness_status=ReconciliationStatus.UNIVERSE_UNAVAILABLE,
        degradation_reason_codes=("UNIVERSE_MEMBERSHIP_UNAVAILABLE",),
        members=(),
        limit_up_members=(),
        limit_down_members=(),
        limit_up_reconciliation=(),
        limit_down_reconciliation=(),
        membership_digest=digest,
        reconciliation_digest=digest,
        business_identity=digest,
        price_evidence=(),
    )
