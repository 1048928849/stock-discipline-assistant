from __future__ import annotations

from datetime import date

import pytest

from app.config import Settings
from app.providers.selected_stock_history import SelectedStockPublicHistoryProvider
from app.selected_stock.contracts import SelectedStockAnalysisRequest
from app.selected_stock.service import SelectedStockAnalysisService


ANALYSIS_DATE = date(2026, 7, 31)
SYMBOLS = ("300308", "300502", "000938", "600519", "920985")


@pytest.mark.selected_stock_integration
def test_real_public_history_covers_selected_stocks_with_stable_lineage():
    provider = SelectedStockPublicHistoryProvider(
        Settings(selected_stock_history_worker_timeout_seconds=35)
    )
    for symbol in SYMBOLS:
        first = provider.get_history(symbol, date(2025, 6, 1), ANALYSIS_DATE)
        second = provider.get_history(symbol, date(2025, 6, 1), ANALYSIS_DATE)

        assert len(first) >= 250
        assert first[-1].trade_date == ANALYSIS_DATE
        assert first[-1].price_unit == "CNY"
        assert first[-1].volume_unit == "share"
        assert first.provider_lineage["response_digest"] == second.provider_lineage[
            "response_digest"
        ]
        assert [item.trade_date for item in first] == sorted(
            {item.trade_date for item in first}
        )


@pytest.mark.selected_stock_integration
def test_real_selected_stock_service_builds_five_deterministic_advisory_plans(session):
    service = SelectedStockAnalysisService(session)

    for symbol in SYMBOLS:
        request = SelectedStockAnalysisRequest(
            stock_code=symbol,
            analysis_date=ANALYSIS_DATE,
        )
        first = service.analyze(request)
        second = service.analyze(request)

        assert first.analysis_run_id == second.analysis_run_id
        assert first.snapshot_hash == second.snapshot_hash
        assert first.result_digest == second.result_digest
        assert first.analysis_date == ANALYSIS_DATE
        stock_lineage = next(
            item
            for item in first.source_lineage
            if item.capability == "market.daily.qfq"
        )
        assert stock_lineage.row_count >= 250
        assert first.technical_evidence["relative_strength"]["stock_vs_csi300_20"] is not None
        assert first.quality_bindings
        assert any(
            item.capability == "market.daily.qfq" for item in first.quality_bindings
        )
        assert any(
            item.capability == "market.index_daily" for item in first.quality_bindings
        )
        assert first.product_v1_comparison.formal_execution_owner == "PRODUCT_V1"
        assert first.technical_evidence["product_v1_shadow_signal_hash"]
        assert first.executable is False
