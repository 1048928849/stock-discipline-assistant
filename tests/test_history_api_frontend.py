from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.models import HistoricalDataBootstrapItem, HistoricalDataBootstrapRun


ROOT = Path(__file__).resolve().parents[1]


def test_history_bootstrap_api_rejects_arbitrary_provider_inputs(client):
    response = client.post(
        "/api/history/bootstrap/discovery",
        json={"trade_date": "2026-07-24", "force_refresh": False, "url": "x"},
    )
    assert response.status_code == 422


def test_history_bootstrap_read_endpoints_serialize_runs_and_items(client, session):
    run = HistoricalDataBootstrapRun(
        purpose="CANDIDATE_DISCOVERY",
        market="CN-A",
        trade_date=date(2026, 7, 24),
        status="BLOCKED",
        provider_id="freestockdb",
        adapter_version="1.0.0",
        config_hash="a" * 64,
        plan_hash="b" * 64,
        required_symbols=["CSI000300"],
        ready_symbols=0,
        failed_symbols=1,
        benchmark_ready=False,
        total_rows_written=0,
        coverage_ratio=Decimal("0"),
        started_at=datetime(2026, 7, 24, 11, 0),
        completed_at=datetime(2026, 7, 24, 11, 1),
        blocked_reasons=["BENCHMARK_HISTORY_MISSING"],
        created_at=datetime(2026, 7, 24, 11, 0),
    )
    session.add(run)
    session.flush()
    session.add(
        HistoricalDataBootstrapItem(
            run_id=run.id,
            symbol="CSI000300",
            capability="market.index_daily",
            adjustment="unadjusted",
            status="FAILED",
            requested_start=date(2026, 3, 1),
            requested_end=date(2026, 7, 24),
            rows_received=0,
            rows_written=0,
            error_code="MISSING",
            error_message="fixture",
        )
    )
    session.commit()

    assert client.get("/api/history/bootstrap/runs").status_code == 200
    assert client.get(f"/api/history/bootstrap/runs/{run.id}").status_code == 200
    items = client.get(f"/api/history/bootstrap/runs/{run.id}/items")
    assert items.status_code == 200
    assert items.json()[0]["capability"] == "market.index_daily"
    assert client.get("/api/history/status/discovery").status_code == 200


def test_opportunity_page_only_offers_history_bootstrap_for_supported_blocks():
    script = (ROOT / "app" / "static" / "discovery.js").read_text(encoding="utf-8")
    template = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    for reason in (
        "BENCHMARK_HISTORY_MISSING",
        "HISTORICAL_UNIVERSE_COVERAGE_INSUFFICIENT",
        "NO_EXECUTABLE_STOCK_DATA",
    ):
        assert reason in script
    assert "准备历史数据" in template
    assert "/api/history/bootstrap/discovery" in script
    assert "rerun" not in script.lower()


def test_0016_uses_explicit_immutable_table_definitions():
    migration = (
        ROOT / "alembic" / "versions" / "20260728_0016_history_bootstrap.py"
    ).read_text(encoding="utf-8")
    assert "op.create_table" in migration
    assert "Base.metadata" not in migration
    assert "historical_data_bootstrap_runs" in migration
    assert "historical_data_bootstrap_items" in migration
