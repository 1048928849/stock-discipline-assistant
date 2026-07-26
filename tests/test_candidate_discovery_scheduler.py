from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import scheduler as scheduler_module
from app.database import Base
from app.discovery.service import CandidateDiscoveryService
from app.models import CandidateDiscoveryRun, DiscoveryCandidate
from test_candidate_discovery_workflow import _snapshot


class SessionContext:
    def __enter__(self):
        return object()

    def __exit__(self, *args):
        return False


def test_scheduler_runs_after_close_and_relies_on_service_idempotency(monkeypatch):
    calls = []
    leases = []
    now = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scheduler_module, "shanghai_now", lambda: now)
    monkeypatch.setattr(scheduler_module, "SessionLocal", SessionContext)
    monkeypatch.setattr(
        scheduler_module,
        "acquire_monitor_lease",
        lambda db, **kwargs: leases.append(("acquire", kwargs)) or True,
    )
    monkeypatch.setattr(
        scheduler_module,
        "release_monitor_lease",
        lambda db, **kwargs: leases.append(("release", kwargs)) or True,
    )

    class Service:
        def __init__(self, db, calendar):
            self.db = db
            self.calendar = calendar

        def run(self, *, now):
            calls.append(now)
            return SimpleNamespace(id=1)

    monkeypatch.setattr(scheduler_module, "CandidateDiscoveryService", Service)
    scheduler_module.run_candidate_discovery()
    scheduler_module.run_candidate_discovery()
    assert calls == [now, now]
    assert [item[0] for item in leases] == [
        "acquire",
        "release",
        "acquire",
        "release",
    ]
    assert {item[1]["lease_name"] for item in leases} == {"candidate_discovery"}


def test_scheduler_skips_when_database_lease_is_held(monkeypatch):
    calls = []
    now = datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scheduler_module, "shanghai_now", lambda: now)
    monkeypatch.setattr(scheduler_module, "SessionLocal", SessionContext)
    monkeypatch.setattr(scheduler_module, "acquire_monitor_lease", lambda *a, **k: False)
    monkeypatch.setattr(
        scheduler_module,
        "CandidateDiscoveryService",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    scheduler_module.run_candidate_discovery()
    assert calls == []


def test_scheduler_skips_non_trading_day(monkeypatch):
    calls = []
    now = datetime(2026, 7, 25, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scheduler_module, "shanghai_now", lambda: now)
    monkeypatch.setattr(
        scheduler_module,
        "CandidateDiscoveryService",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    scheduler_module.run_candidate_discovery()
    assert calls == []


def test_two_workers_create_one_discovery_run_and_candidate_set(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'concurrent-discovery.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 20},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = Barrier(2)

    def worker():
        with sessions() as db:
            barrier.wait()
            return CandidateDiscoveryService(db).run(
                now=datetime(2026, 7, 24, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
                snapshot=_snapshot(),
            ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        ids = list(executor.map(lambda _: worker(), range(2)))
    with sessions() as db:
        assert len(set(ids)) == 1
        assert db.scalar(select(func.count(CandidateDiscoveryRun.id))) == 1
        assert db.scalar(select(func.count(DiscoveryCandidate.id))) == 1
