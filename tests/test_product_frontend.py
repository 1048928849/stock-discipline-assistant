from pathlib import Path


ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "app" / "templates" / "index.html"
PRODUCT_JS = ROOT / "app" / "static" / "product-v1.js"
PRODUCT_CSS = ROOT / "app" / "static" / "product-v1.css"


def test_formal_pages_enable_complete_product_refresh_by_default():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.count('name="refresh" type="checkbox" checked') == 2


def test_product_renderer_is_loaded_after_legacy_application_script():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert html.index('/static/app.js') < html.index('/static/product-v1.js')


def test_product_assets_have_a_release_cache_key():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert '/static/product-v1.css?v=20260726' in html
    assert '/static/product-v1.js?v=20260726' in html


def test_product_renderer_consumes_complete_api_contract_without_rule_math():
    source = PRODUCT_JS.read_text(encoding="utf-8")
    for field in (
        "market_regime",
        "industry_context",
        "concept_chain_context",
        "technical_context",
        "buy_point_assessment",
        "position_constraints",
        "risk_plan",
        "exit_plan",
        "required_data",
        "decision_package",
        "chain_name",
        "node_name",
        "stage",
    ):
        assert field in source
    for prohibited in (
        "final_allowed_quantity *",
        "hard_stop =",
        "buy_zone =",
        "per_share_risk =",
    ):
        assert prohibited not in source


def test_product_renderer_keeps_confirm_authority_on_server():
    source = PRODUCT_JS.read_text(encoding="utf-8")
    assert "confirm-one-click-plan" in source
    assert "freeze_allowed" in source


def test_product_renderer_displays_strategy_binding_lineage():
    source = PRODUCT_JS.read_text(encoding="utf-8")
    for field in (
        "strategy_bindings",
        "strategy_id",
        "strategy_version",
        "implementation_hash",
        "parameter_hash",
        "signal_hash",
        "binding_hash",
    ):
        assert field in source


def test_product_renderer_labels_optional_quality_items():
    source = PRODUCT_JS.read_text(encoding="utf-8")
    assert 'item.required ? "REQUIRED" : "OPTIONAL"' in source


def test_product_layout_wraps_long_provider_failures():
    source = PRODUCT_CSS.read_text(encoding="utf-8")
    assert ".pipeline-step small" in source
    assert "overflow-wrap:anywhere" in source


def test_product_mobile_layout_keeps_primary_navigation_compact():
    source = PRODUCT_CSS.read_text(encoding="utf-8")
    assert "repeat(3,minmax(0,1fr))" in source
    assert ".sidebar .nav-group,.sidebar .docs{display:none}" in source
