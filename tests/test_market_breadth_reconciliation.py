from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.market_breadth.reconciliation import (
    CANONICAL_A_SHARE_SH_SZ_V1,
    Board,
    BreadthPriceEvidence,
    Exchange,
    MarketUniverseMembership,
    MembershipQuality,
    PoolReconciliationClass,
    ReconciliationClass,
    ReconciliationStatus,
    SecurityType,
    TradableStatus,
    classify_symbol,
    reconcile_market_breadth_universe,
)


DAY = date(2026, 7, 24)
NOW = datetime(2026, 7, 24, 15, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
SPEC = CANONICAL_A_SHARE_SH_SZ_V1


def member(
    symbol: str,
    *,
    listing: date = date(2000, 1, 1),
    delisting: date | None = None,
    security_type: SecurityType = SecurityType.COMMON_STOCK,
    status: TradableStatus = TradableStatus.ACTIVE,
    spec=SPEC,
):
    exchange, board = classify_symbol(symbol)
    return MarketUniverseMembership.build(
        spec=spec,
        trade_date=DAY,
        symbol=symbol,
        exchange=exchange,
        board=board,
        security_type=security_type,
        tradable_status=status,
        listing_date=listing,
        delisting_date=delisting,
        source="instrument-master",
        source_reference=f"master:{symbol}",
        observed_at=NOW,
    )


def price(
    symbol: str,
    *,
    day: date = DAY,
    close: str = "11",
    previous: str = "10",
    source: str = "primary",
    unit: str = "CNY",
    adjustment: str = "unadjusted_or_daily_return_compatible",
):
    return BreadthPriceEvidence(
        symbol=symbol,
        trade_date=day,
        close=Decimal(close),
        previous_close=Decimal(previous),
        source=source,
        source_reference=f"{source}:{symbol}:{day}",
        price_unit=unit,
        adjustment=adjustment,
    )


def report(members, primary, **kwargs):
    return reconcile_market_breadth_universe(
        spec=kwargs.pop("spec", SPEC),
        trade_date=DAY,
        membership=members,
        primary=primary,
        **kwargs,
    )


def classification(result, symbol):
    return next(item.classification for item in result.members if item.symbol == symbol)


def test_01_explicit_universe_version():
    assert (SPEC.universe_id, SPEC.version) == ("A_SHARE_SH_SZ", "1.0.0")
    assert SPEC.membership_quality == MembershipQuality.SINGLE_SOURCE


@pytest.mark.parametrize(
    ("symbol", "exchange", "board"),
    [
        ("600001", Exchange.SH, Board.SH_MAIN),
        ("688001", Exchange.SH, Board.SH_STAR),
        ("000001", Exchange.SZ, Board.SZ_MAIN),
        ("300001", Exchange.SZ, Board.SZ_CHINEXT),
        ("920176", Exchange.BSE, Board.BSE),
        ("999999", Exchange.UNKNOWN, Board.UNKNOWN),
    ],
)
def test_02_exchange_and_board_classification(symbol, exchange, board):
    assert classify_symbol(symbol) == (exchange, board)


def test_03_etf_is_excluded():
    result = report([member("600001", security_type=SecurityType.ETF)], [])
    assert result.expected_member_count == 0
    assert classification(result, "600001") == ReconciliationClass.NOT_IN_UNIVERSE


def test_04_bse_is_explicitly_outside_sh_sz_scope():
    result = report([member("920176")], [], raw_limit_up=["920176"])
    assert result.pool_symbols_outside_scope == ("920176",)
    assert result.completeness_status == ReconciliationStatus.COMPLETE


def test_05_not_yet_listed_on_historical_date_is_excluded():
    result = report([member("301583", listing=date(2026, 7, 25))], [])
    assert classification(result, "301583") == ReconciliationClass.NOT_LISTED_ON_DATE


def test_06_delisted_before_date_is_excluded():
    result = report([member("600001", delisting=DAY)], [])
    assert classification(result, "600001") == ReconciliationClass.NOT_LISTED_ON_DATE


def test_07_duplicate_membership_is_rejected():
    result = report([member("600001"), member("600001")], [price("600001")])
    assert result.completeness_status == ReconciliationStatus.INCOMPLETE
    assert result.membership_conflicts == ("600001",)


def test_08_unknown_board_is_degraded():
    unknown = replace(member("600001"), exchange=Exchange.UNKNOWN, board=Board.UNKNOWN)
    result = report([unknown], [])
    assert result.completeness_status == ReconciliationStatus.INCOMPLETE
    assert classification(result, "600001") == ReconciliationClass.INVALID_SECURITY


def test_09_complete_primary_coverage():
    result = report([member("600001"), member("300001")], [price("600001"), price("300001")])
    assert result.completeness_status == ReconciliationStatus.COMPLETE
    assert result.primary_rows == 2


def test_10_one_real_member_missing():
    result = report([member("600001"), member("300001")], [price("600001")])
    assert result.unresolved_missing_members == ("300001",)
    assert not result.persistable


@pytest.mark.parametrize(
    "bad_price",
    [
        price("600001", close="0"),
        price("600001", previous="0"),
        price("600001", close="NaN"),
    ],
)
def test_11_invalid_price_security_is_missing(bad_price):
    result = report([member("600001")], [bad_price])
    assert result.completeness_status == ReconciliationStatus.INCOMPLETE


def test_12_duplicate_primary_symbol_is_rejected():
    result = report([member("600001")], [price("600001"), price("600001")])
    assert "primary:600001" in result.membership_conflicts


def test_13_wrong_primary_date_is_rejected():
    result = report([member("600001")], [price("600001", day=date(2026, 7, 23))])
    assert not result.persistable


def test_14_stale_current_provider_is_not_a_reconciliation_pass():
    result = report([member("600001")], [price("600001", day=date(2026, 7, 23))])
    assert "BREADTH_UNIVERSE_INCOMPLETE" in result.degradation_reason_codes


def test_15_valid_supplementary_fill():
    result = report(
        [member("301583")],
        [],
        supplementary=[price("301583", source="trusted-supplement", adjustment="qfq")],
    )
    assert result.completeness_status == ReconciliationStatus.COMPLETE_WITH_SUPPLEMENTARY_DATA
    assert classification(result, "301583") == ReconciliationClass.PRESENT_SUPPLEMENTARY


@pytest.mark.parametrize(
    "supplement",
    [
        price("301583", day=date(2026, 7, 23), source="supplement", adjustment="qfq"),
        replace(price("301583", source="supplement", adjustment="qfq"), symbol="300001"),
        price("301583", source="supplement", unit="USD", adjustment="qfq"),
        price("301583", source="supplement", adjustment="unknown"),
    ],
)
def test_16_to_18_invalid_supplement_is_rejected(supplement):
    result = report([member("301583")], [], supplementary=[supplement])
    assert result.completeness_status == ReconciliationStatus.INCOMPLETE


def test_19_supplementation_appears_in_lineage_contract():
    result = report(
        [member("301583")],
        [],
        supplementary=[price("301583", source="supplement", adjustment="qfq")],
    )
    item = next(item for item in result.members if item.symbol == "301583")
    assert item.reason == "BREADTH_PRIMARY_PROVIDER_GAP_SUPPLEMENTED"
    assert result.supplementary_rows == 1


def test_20_suspended_member_is_deterministically_excluded():
    result = report([member("600001", status=TradableStatus.SUSPENDED)], [])
    assert result.persistable
    assert result.excluded_legitimate_members == 1
    assert classification(result, "600001") == ReconciliationClass.SUSPENDED_NO_BAR


def test_21_suspended_member_gets_no_invented_return():
    result = report([member("600001", status=TradableStatus.SUSPENDED)], [])
    assert result.price_evidence == ()
    assert result.active_trading_member_count == 0


def test_22_in_scope_pool_symbol():
    result = report([member("600001")], [price("600001")], raw_limit_up=["600001"])
    assert result.limit_up_members == ("600001",)


def test_23_out_of_scope_pool_symbol():
    result = report([member("600001")], [price("600001")], raw_limit_up=["920176"])
    item = result.limit_up_reconciliation[0]
    assert item.classification == PoolReconciliationClass.OUTSIDE_UNIVERSE


def test_24_in_scope_pool_missing_cross_section_blocks():
    result = report([member("301583")], [], raw_limit_up=["301583"])
    assert not result.persistable
    assert (
        result.limit_up_reconciliation[0].classification
        == PoolReconciliationClass.MISSING_FROM_CROSS_SECTION
    )


def test_25_duplicate_pool_symbol_blocks():
    result = report([member("600001")], [price("600001")], raw_limit_up=["600001", "600001"])
    assert not result.persistable


def test_26_raw_and_filtered_pool_counts_are_preserved():
    result = report([member("600001")], [price("600001")], raw_limit_up=["600001", "920176"])
    assert (result.limit_up_raw_count, result.limit_up_in_scope_count) == (2, 1)


@pytest.mark.parametrize(
    ("members", "primary", "supplementary", "expected"),
    [
        ([member("600001")], [price("600001")], [], ReconciliationStatus.COMPLETE),
        (
            [member("600001")],
            [],
            [price("600001", source="supplement", adjustment="qfq")],
            ReconciliationStatus.COMPLETE_WITH_SUPPLEMENTARY_DATA,
        ),
        ([member("600001")], [], [], ReconciliationStatus.INCOMPLETE),
        (None, [], [], ReconciliationStatus.UNIVERSE_UNAVAILABLE),
    ],
)
def test_27_to_30_all_reconciliation_statuses(members, primary, supplementary, expected):
    result = report(members, primary, supplementary=supplementary)
    assert result.completeness_status == expected


def test_31_unresolved_member_blocks_persistence():
    assert not report([member("600001")], []).persistable


def test_32_301583_style_case_is_supplemented_not_ignored():
    result = report(
        [member("301583")],
        [],
        supplementary=[price("301583", source="supplement", adjustment="qfq")],
        raw_limit_up=["301583"],
    )
    assert result.persistable and result.limit_up_in_scope_count == 1


def test_33_920176_style_case_is_outside_sh_sz_but_recorded():
    result = report([member("600001")], [price("600001")], raw_limit_up=["920176"])
    assert result.persistable
    assert result.pool_symbols_outside_scope == ("920176",)
    assert result.limit_up_raw_count == 1 and result.limit_up_in_scope_count == 0


def test_34_fetched_at_does_not_change_business_identity():
    first = report([member("600001")], [price("600001")])
    later_member = replace(member("600001"), observed_at=NOW.replace(hour=16))
    second = report([later_member], [price("600001")])
    assert first.business_identity == second.business_identity


def test_35_membership_change_affects_digest():
    first = report([member("600001")], [price("600001")])
    second = report([member("600001"), member("300001")], [price("600001"), price("300001")])
    assert first.membership_digest != second.membership_digest


def test_36_universe_version_affects_identity():
    first = report([member("600001")], [price("600001")])
    spec2 = replace(SPEC, version="1.0.1")
    second = report([member("600001", spec=spec2)], [price("600001")], spec=spec2)
    assert first.business_identity != second.business_identity


def test_37_incomplete_breadth_is_not_neutral():
    result = report([member("600001")], [])
    assert result.completeness_status == ReconciliationStatus.INCOMPLETE
    assert "NEUTRAL" not in result.degradation_reason_codes


def test_38_incomplete_breadth_cannot_increase_risk():
    result = report([member("600001")], [])
    assert result.persistable is False


def test_39_product_v1_golden_document_unchanged():
    text = open("docs/strategy/product_v1_rule_inventory.md", encoding="utf-8").read()
    for value in ("READY", "[10.4209, 10.5391]", "9.7023", "600", "100", "0.7777", "77.77"):
        assert value in text


def test_40_csv_v2_authority_is_not_part_of_breadth_report():
    result = report([member("600001")], [price("600001")])
    assert not hasattr(result, "executable_strategy")


def test_41_p1t_veto_authority_is_not_part_of_breadth_report():
    result = report([member("600001")], [price("600001")])
    assert not hasattr(result, "override_veto")
