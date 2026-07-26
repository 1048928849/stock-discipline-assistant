from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
SOURCE = (ROOT / "app/static/discovery.js").read_text(encoding="utf-8")
CSS = (ROOT / "app/static/discovery.css").read_text(encoding="utf-8")


def test_opportunity_pool_is_a_real_application_page():
    assert 'href="/opportunities"' in TEMPLATE
    assert 'data-view="opportunities"' in TEMPLATE
    assert "/static/discovery.js?v=20260727" in TEMPLATE


def test_opportunity_pool_displays_required_quality_and_metrics():
    for field in (
        "market_state",
        "quality_status",
        "candidate_type",
        "return_5d_pct",
        "return_20d_pct",
        "distance_ma20_pct",
        "net_inflow_1d",
        "broken_limit_rate",
        "risk_flags",
    ):
        assert field in SOURCE


def test_opportunity_pool_has_no_recommended_buy_action():
    assert "推荐买入" not in TEMPLATE
    assert "推荐买入" not in SOURCE
    assert "研究并加入观察" in TEMPLATE


def test_opportunity_pool_mobile_layout_supports_390_width():
    assert "@media(max-width:520px)" in CSS
    assert ".candidate-metrics{grid-template-columns:1fr 1fr}" in CSS
