from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from app.selected_stock.contracts import (
    ContextStatus,
    DataStatus,
    SelectedStockAnalysisRequest,
)
from app.selected_stock.indicators import calculate_indicators
from app.selected_stock.strategy import CycleStructureValidationStrategyV2


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 31, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _rows(offset: Decimal = Decimal("0")):
    rows = []
    start = date(2025, 10, 1)
    for index in range(260):
        day = start + timedelta(days=index)
        close = Decimal("10") + Decimal(index) / Decimal("100") + offset
        rows.append(
            {
                "trade_date": day,
                "open": close - Decimal("0.05"),
                "high": close + Decimal("0.10"),
                "low": close - Decimal("0.10"),
                "close": close,
                "volume": Decimal("100000") + Decimal(index),
            }
        )
    return rows


def _result(payload: SelectedStockAnalysisRequest):
    indicators = calculate_indicators(
        _rows(),
        benchmark_rows=_rows(Decimal("100")),
    )
    return CycleStructureValidationStrategyV2().build_plan(
        request=payload,
        generated_at=NOW,
        analysis_date=date(2026, 6, 17),
        data_status=DataStatus.FRESH,
        market_status=ContextStatus.BREADTH_UNAVAILABLE,
        industry_status=ContextStatus.INDUSTRY_CONTEXT_UNAVAILABLE,
        industry_name=None,
        stock_row_count=260,
        indicators=indicators,
        quality_bindings=(),
        source_lineage=(),
        snapshot_hash="d" * 64,
        product_v1_status="FORMAL_EXECUTION_REMAINS_PRODUCT_V1",
    )


def test_selected_stock_api_success_without_llm(client, monkeypatch):
    monkeypatch.setattr(
        "app.api.selected_stock.SelectedStockAnalysisService.analyze",
        lambda self, payload: _result(payload),
    )
    response = client.post(
        "/api/v1/selected-stock-analysis",
        json={"stock_code": "300308", "strategy_mode": "CSV_V2_ADVISORY"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["strategy_id"] == "cycle_structure_validation_v2"
    assert body["executable"] is False
    assert body["market_context_status"] == "BREADTH_UNAVAILABLE"
    assert body["industry_context_status"] == "INDUSTRY_CONTEXT_UNAVAILABLE"
    assert body["product_v1_comparison"]["formal_execution_owner"] == "PRODUCT_V1"


def test_selected_stock_api_rejects_partial_user_price(client):
    response = client.post(
        "/api/v1/selected-stock-analysis",
        json={"stock_code": "300308", "current_price": 20},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_selected_stock_page_contains_required_states_and_fields(client):
    response = client.get("/selected-stock-analysis")
    assert response.status_code == 200
    text = response.text
    for token in (
        "selected-stock-form",
        "stock_code",
        "analysis_date",
        "current_price_observed_at",
        "account_size",
        "current_position_quantity",
        "average_cost",
        "max_position_pct",
        "risk_budget",
        "selected-stock-result",
    ):
        assert token in text


def test_frontend_explicitly_handles_blocked_and_missing_states():
    script = (ROOT / "app" / "static" / "selected-stock.js").read_text(encoding="utf-8")
    style = (ROOT / "app" / "static" / "selected-stock.css").read_text(encoding="utf-8")
    for token in (
        "NO_TRADE",
        "WAIT_FOR_TRIGGER",
        "STALE_ONE_SESSION",
        "BREADTH_UNAVAILABLE",
        "INDUSTRY_CONTEXT_UNAVAILABLE",
        "quality_bindings",
        "source_lineage",
        "product_v1_comparison",
    ):
        assert token in script
    assert ".selected-decision.blocked" in style
    assert "#a72525" in style


def test_rule_matrix_is_complete_and_raw_docx_is_ignored():
    matrix = (ROOT / "docs" / "strategy" / "huiyang_rule_matrix.md").read_text(
        encoding="utf-8"
    )
    for category in (
        "DATA_GATE",
        "MARKET_CYCLE",
        "MAIN_THEME",
        "INDUSTRY_CONTINUITY",
        "STOCK_ROLE",
        "RELATIVE_STRENGTH",
        "TREND_STRUCTURE",
        "MOMENTUM",
        "VOLUME_PRICE",
        "SUPPORT_RESISTANCE",
        "INTRADAY_ACCEPTANCE",
        "ENTRY_TRIGGER",
        "POSITION_RULE",
        "ADD_RULE",
        "REDUCE_RULE",
        "EXIT_RULE",
        "DAILY_LOSS_RULE",
        "T1_RULE",
        "CATALYST_RULE",
        "NON_DETERMINISTIC",
        "REJECTED",
    ):
        assert f"| {category} |" in matrix
    assert "logical page" in matrix
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".codex-input/*.docx"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not tracked.stdout.strip()

    docx_files = list((ROOT / ".codex-input").glob("*.docx"))
    if not docx_files:
        return
    ignored = subprocess.run(
        ["git", "check-ignore", *[str(path) for path in docx_files]],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0
