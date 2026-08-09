import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "valuation" / "p1v_m0_recovery_manifest.json"
MATRIX_PATH = ROOT / "docs" / "valuation" / "p1v_m0_recovery_matrix.md"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_three_stock_identifiers_are_fixed() -> None:
    stocks = _manifest()["stocks"]
    assert [(stock["stock_code"], stock["company"]) for stock in stocks] == [
        ("300308", "中际旭创"),
        ("600406", "国电南瑞"),
        ("002112", "三变科技"),
    ]


def test_evidence_uses_only_allowed_classifications() -> None:
    allowed = {
        "PRIMARY_CALCULATION_EVIDENCE",
        "DERIVED_SUMMARY",
        "SECONDARY_REFERENCE",
        "UNRELATED",
    }
    assert {item["classification"] for item in _manifest()["evidence"]} <= allowed


def test_no_inferred_number_is_marked_golden() -> None:
    for stock in _manifest()["stocks"]:
        assert stock["inferred"] == []
        assert stock["golden_values"] == []


def test_historical_and_current_contexts_are_separate() -> None:
    contexts = _manifest()["contexts"]
    assert set(contexts) == {"HISTORICAL_REPRODUCTION", "CURRENT_ANALYSIS"}
    assert "immutable original" in contexts["HISTORICAL_REPRODUCTION"]["description"]
    assert "current trusted data" in contexts["CURRENT_ANALYSIS"]["description"]


def test_missing_critical_input_keeps_golden_incomplete() -> None:
    method = _manifest()["method"]
    assert method["missing_critical_fields"]
    assert method["golden_complete"] is False
    assert method["version"] == "UNRESOLVED"
    assert _manifest()["status"] == "VALUATION_GOLDEN_PARTIAL"


def test_every_evidence_item_has_source_location() -> None:
    for item in _manifest()["evidence"]:
        assert item["source_artifact"]
        assert item["source_location"]


def test_no_deterministic_golden_formula_is_claimed_without_primary_evidence() -> None:
    method = _manifest()["method"]
    assert method["formula_status"] == "DERIVED_SUMMARY_ONLY"
    assert not any(
        item["classification"] == "PRIMARY_CALCULATION_EVIDENCE"
        for item in _manifest()["evidence"]
    )


def test_no_golden_rounding_is_claimed() -> None:
    assert "rounding_rules" in _manifest()["method"]["missing_critical_fields"]
    assert all(stock["golden_values"] == [] for stock in _manifest()["stocks"])


def test_current_mode_cannot_mutate_a_historical_fixture() -> None:
    contexts = _manifest()["contexts"]
    assert contexts["HISTORICAL_REPRODUCTION"]["available"] is False
    assert contexts["CURRENT_ANALYSIS"]["available"] is False
    assert not (ROOT / "tests" / "fixtures" / "valuation").exists()


def test_method_version_cannot_be_assigned_while_formula_is_unresolved() -> None:
    method = _manifest()["method"]
    assert method["method_id"] == "ORIGINAL_EPS_PE_TARGET_PRICE"
    assert method["version"] == "UNRESOLVED"


def test_valuation_has_no_execution_authority() -> None:
    authority = _manifest()["authority"]
    assert authority["classification"] == "RESEARCH_EVIDENCE"
    assert authority["executable"] is False
    assert authority["high_upside_is_execution_permission"] is False
    assert "create ENTRY_ALLOWED" in authority["cannot"]
    assert "override NO_TRADE" in authority["cannot"]


def test_csv_v2_and_p1t_veto_authority_are_preserved() -> None:
    forbidden = _manifest()["authority"]["cannot"]
    assert "make CSV_V2 executable" in forbidden
    assert "override a P1T hard veto" in forbidden


def test_recovery_matrix_records_all_required_fields() -> None:
    matrix = MATRIX_PATH.read_text(encoding="utf-8")
    for field in (
        "Calculation date",
        "Reference stock price",
        "Financial data as-of",
        "Forecast year(s)",
        "Revenue if used",
        "Net profit if used",
        "Total shares if used",
        "Forecast EPS",
        "EPS derivation",
        "PE assumptions",
        "Scenario assumptions",
        "Target price",
        "Upside/downside",
        "Rounding",
    ):
        assert field in matrix


def test_product_v1_golden_contract_remains_unchanged() -> None:
    inventory = (ROOT / "docs" / "strategy" / "product_v1_rule_inventory.md").read_text(
        encoding="utf-8"
    )
    for expected in (
        "| `rule_status` | `READY` |",
        "| buy zone | `[10.4209, 10.5391]` |",
        "| hard stop | `9.7023` |",
        "| final quantity | `600` |",
        "| trial quantity | `100` |",
        "| per-share risk | `0.7777` |",
        "| maximum loss | `77.77` |",
    ):
        assert expected in inventory
