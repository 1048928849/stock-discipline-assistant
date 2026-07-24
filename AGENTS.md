# Codex Engineering Guardrails

This file is the mandatory entry point for repository work. Detailed quality semantics and
the end-to-end test matrix live in
[`docs/architecture/quality-invariants.md`](docs/architecture/quality-invariants.md).

## Core invariants

1. `CONFLICTED`, `STALE`, and `MISSING` data must never produce an executable formal plan.
2. A cache may preserve or lower quality; it must never raise quality.
3. Every quality conclusion must bind `capability`, `subject`, and `observed_at`.
4. Analysis, UI display, persistence, and freeze must use the same effective-quality algorithm.
5. AI must not change rule status, buy zones, positions, quantities, or hard stops.
6. One `analysis_run` may produce at most one formal plan.
7. One account, symbol, and `plan_version` tuple may identify at most one record.
8. Every formal-plan entry point must call the unified freeze service.

## Development workflow

1. Use Ask Mode for impact analysis before large changes; Ask Mode must not modify code.
2. Address one cross-cutting invariant at a time.
3. Add a test that fails against the old implementation before changing production code.
4. Do not set a final `quality_status` directly to fabricate a passing test.
5. Do not monkeypatch the final pipeline step or `DecisionPackage` to claim production coverage.
6. Concurrency tests must use independent SQLAlchemy Sessions.
7. Validate SQLite and MySQL separately.
8. Do not announce completion until remote CI passes.

## Required surfaces

Inspect Provider, `DataHubRouter`, database persistence, cache restoration, API, scheduler,
background tasks, `DecisionPackage`, freeze/confirm, and frontend display for every quality
change.

## Fixed verification

Run from the repository root:

```powershell
python -m pytest
python -m ruff check .
git diff --check
python -m alembic heads
python -c "from app.main import app; print(len(app.openapi()['paths']))"
python -c "from app.main import app; print(app.title)"
$env:DATABASE_URL="sqlite://"; python -m alembic upgrade head
$env:DATABASE_URL=$env:MYSQL_TEST_DATABASE_URL; python -m alembic upgrade head
python -m pytest -m mysql_integration
```

The MySQL commands require an isolated MySQL 8 database in `MYSQL_TEST_DATABASE_URL`.

## Prohibited

- Do not modify `main`, merge automatically, or mark a Draft PR ready for review.
- Do not start Phase 2 features.
- Do not change the fixed deterministic trading-rule results.
