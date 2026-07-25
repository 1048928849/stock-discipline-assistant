import os

os.environ.setdefault("NUMBA_DISABLE_JIT", "1")
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["LLM_PROVIDER"] = "openai_compatible"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_MODEL"] = ""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app


@pytest.fixture(autouse=True)
def deterministic_legacy_market_session(request, monkeypatch):
    """Keep pre-D.1 tests independent from the wall clock.

    D.1 contract tests inject their own clock and exercise the production
    validators without this compatibility fixture.
    """
    real_clock_tests = {
        "test_freeze_market_validation_uses_shanghai_clock",
        "test_confirm_at_utc_0200_is_treated_as_shanghai_morning",
        "test_confirm_at_utc_0700_is_treated_as_shanghai_close",
    }
    if (
        request.path.name == "test_market_time_contracts.py"
        or request.node.name in real_clock_tests
    ):
        yield
        return

    from app.data_hub.trading_calendar import TradingPhase, XSHGTradingCalendar

    monkeypatch.setattr(
        XSHGTradingCalendar,
        "market_phase",
        lambda self, now=None: TradingPhase.MORNING_SESSION,
    )
    yield


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        yield db
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(session):
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
