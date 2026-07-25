# Codex Engineering Guardrails

This file is the mandatory repository entry point. Detailed quality semantics and the
end-to-end matrix live in
[`docs/architecture/quality-invariants.md`](docs/architecture/quality-invariants.md).

## 1. Core execution

- Implement only the explicit task after inspecting the complete formal call chain.
- Prefer the smallest safe, testable change. Do not refactor accepted architecture or prebuild
  future phases.
- Close one class of defect across every formal entry and exit in the same task; do not split a
  fix that can be completed together.
- For time, persistence, quality, or binding changes, inspect all equivalent production paths,
  not only the reported failure.

## 2. Permanent prohibitions

### Persistence and Provider lineage

The formal order is: Provider -> Router audit -> payload/digest/row-count/exact-lineage
validation -> atomic business persistence -> `mark_persisted`.

- Before unified persistence validation, do not add or mutate business rows, update raw data or
  source timestamps, or flush unvalidated business data.
- Failure must preserve the complete old cache and leave the new quality record
  `persisted=false`.
- Never select persistence input through `provider.calls`, a capability's last call, or another
  ambiguous cache. Use the exact `ProviderResult` returned by the current formal call.
- Validate capability, Subject, semantic key, row count, normalized and ProviderResult digests,
  exact `quality_record_id`, persisted state, and payload snapshot. Cache fallback remains bound
  to the old quality record, never a failed new record.

### Market and time

- `observed_at` is business market time and `fetched_at` is request completion time; never
  substitute one for the other.
- Quote times must be aware. Reject naive Quote times. Realtime/latest-close/qfq/unadjusted must
  match their capability; incomplete current-day bars and realtime quotes outside continuous
  sessions are not executable.
- Provider and business calculations use aware times; `market.*` storage is Shanghai wall-time
  naive; Research and other non-market storage is UTC naive; API, Evidence, and DecisionPackage
  authority times are aware.
- Host-local `datetime.now()` must not enter formal decisions, and Host-local `date.today()` must
  not define a business scope. Do not mix UTC-naive and Shanghai-naive values, compare aware with
  naive, or use undeclared `replace(tzinfo=...)` semantics.
- `run_one_click_analysis` captures one `analysis_started_at`. Market date, announcement window,
  Research refresh/inventory, Evidence, preview, and DecisionPackage time derive from that one
  instant. Do not recalculate the current date during the analysis.
- Host timezone must not affect announcement Subject/semantic key/scan scope, quality bindings,
  Evidence times or digest, preview/package hash, expiry, or confirm/freeze.

### Quality, execution, and AI

- `CONFLICTED`, `STALE`, and `MISSING` never produce an executable formal plan; cache may preserve
  or lower quality, never raise it.
- Every quality conclusion binds capability, subject, and observed time. Analysis, UI,
  persistence, and freeze use the same effective-quality algorithm.
- One analysis produces at most one formal plan; one account/symbol/version tuple identifies at
  most one row; every formal entry uses the unified freeze service.
- AI cannot change rule status, buy zone, position, quantity, hard stop, or execution permission.

### Git and fixed results

- Work only on `agent/stock-discipline-os-core`. Do not modify `main`, merge, mark the Draft PR
  ready, make the repository public, or enter a later phase early.
- Never report an old SHA as a new result when no commit was created.
- Preserve: `READY`, buy zone `[10.4209, 10.5391]`, hard stop `9.7023`, final quantity `600`,
  trial quantity `100`, per-share risk `0.7777`, and maximum loss `77.77`.

## 3. Formal call-chain review

List the task's formal call chain before changing production logic. For announcement work,
inspect: one-click analysis -> Research refresh/sync -> Router and Subject -> persistence ->
selector/inventory -> source binding -> Evidence/digests/package hash -> confirm/freeze.

## 4. Work budget

- Locate and confirm the chain within 5 minutes; production implementation within 20 minutes;
  targeted tests within 15 minutes; self-review and commit within 10 minutes. Command runtime is
  excluded.
- Do not explore without a boundary or run the full suite after every small edit.
- Make at most two evidence-based fixes for the same failing test; on a third failure, stop random
  edits and report the root cause.

## 5. Verification order

1. New targeted tests.
2. Related call-chain regressions.
3. Current-phase regressions.
4. Full `python -m pytest -q` at most once.
5. `python -m ruff check .`.
6. `git diff --check`.
7. Fixed-number golden regression.

Run SQLite/MySQL migration matrices only for model, migration, constraint, or index changes.
Regenerate OpenAPI only for route or schema changes. SQLite and MySQL validations are separate;
do not claim completion before real remote CI succeeds.

## 6. Completion review

Before commit, search prohibited patterns, recheck the complete formal chain and file scope,
remove debug or unrelated formatting, verify fixed trading results, and confirm no later-phase
work was added. Keep the completion report under 800 Chinese characters.
