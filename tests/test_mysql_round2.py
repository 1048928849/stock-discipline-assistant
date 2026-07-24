import os

import pytest
from sqlalchemy import create_engine, inspect

from app.database import Base


@pytest.mark.mysql_integration
def test_mysql8_quality_and_uniqueness_schema_can_be_enabled_in_ci():
    url = os.getenv("MYSQL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MYSQL_TEST_DATABASE_URL is not configured")
    engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(engine)
    tables = set(inspect(engine).get_table_names())
    assert "data_quality_records" in tables
    constraints = {
        item["name"] for item in inspect(engine).get_unique_constraints("trade_plans")
    }
    assert "uq_trade_plan_analysis_run" in constraints
    assert "uq_trade_plan_account_symbol_version" in constraints
