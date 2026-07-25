# Data Quality Invariants

This document defines the system-wide contract for deciding whether data may participate in
analysis, display, persistence, and formal-plan freezing. It is normative: new providers,
entry points, schedulers, and background tasks must obey the same rules.

## 1. Capability and subject

A quality conclusion is scoped to one `(capability, subject, observed_at)` tuple.

- **Capability** names the business meaning and data semantics, not the provider method.
  Examples include `market.daily.qfq`, `market.quote.realtime`,
  `market.quote.latest_close`, `fundamental.profile`, and `announcement.catalog`.
- **Subject** identifies the entity to which the conclusion applies. For a stock this is the
  canonical six-digit symbol; an index, sector, account, or plan must use its own stable
  identifier. The current stock Evidence schema stores this identity in `symbol`; code that
  introduces an explicit `subject` field must preserve that mapping.
- **observed_at** is when the represented business fact was observed. It is not interchangeable
  with provider fetch time, cache-write time, or database update time.

Data with different capabilities or subjects must never be cross-verified. In particular,
qfq bars cannot verify unadjusted bars, and a latest close cannot verify a realtime quote.
Provider name, URL, response ordering, and other transport metadata are audit attributes, not
business identity.

Each persisted quality decision must retain enough lineage to reconstruct the conclusion:
provider, capability, subject, observed/fetched/cached timestamps, normalized digest,
provider observations, conflict fields, units, adjustment, and fallback/cache flags.

## 2. Effective quality

The only executable states are `VERIFIED` and `SINGLE_SOURCE`.
`CONFLICTED`, `STALE`, and `MISSING` always block executable `READY` and formal-plan freezing.
`SINGLE_SOURCE` is intentionally executable under the current Phase 1 policy.

For one capability and subject, effective quality is evaluated as follows:

1. Select only observations with matching business semantics.
2. No usable observation produces `MISSING`.
3. Recompute freshness at the point of use. If every usable observation is expired, produce
   `STALE`.
4. One fresh observation produces `SINGLE_SOURCE`.
5. Multiple fresh canonical observations with one digest produce `VERIFIED`.
6. Multiple fresh canonical observations with different digests produce `CONFLICTED`.
7. Combine required capabilities using the blocking/worst result. Current precedence is
   `MISSING`, `CONFLICTED`, `STALE`, `SINGLE_SOURCE`, `VERIFIED`.

Canonical comparison must normalize Decimal values within policy tolerance, timestamps,
symbols, units, adjustment, field ordering, and capability-specific business fields. It must
exclude source URLs, fetch metadata, and non-business response fields.

## 3. Cache monotonicity

Cache lineage is immutable evidence, not a new provider observation. Reading or rewriting a
cache must not turn `CONFLICTED`, `STALE`, or `MISSING` into `SINGLE_SOURCE` or `VERIFIED`.

The cache rule is monotonic:

```text
effective_cache_quality = worse(stored_quality, freshness_now, current_refresh_signal)
```

A trusted cached value may remain usable only while its own `observed_at` is fresh and no newer
blocking signal applies. `cached_at`, `fetched_at`, a recent database write, or a successful
deserialization never proves freshness or trust.

Untrusted provider output may be persisted to an audit or quarantine record. It must not
overwrite trusted formal market, profile, announcement, financial, or valuation rows.

## 4. Dynamic freshness

Freshness is evaluated whenever data is analyzed, displayed as executable, saved, or frozen.
It is not a permanent property copied from acquisition time.

- Intraday capabilities use their configured duration from `observed_at`.
- `market.quote.realtime` additionally requires an active A-share continuous
  trading session. Fresh age alone cannot make a lunch-break, after-close,
  weekend, or exchange-holiday quote executable.

## Market time and capability contracts

All market decisions use timezone-aware `Asia/Shanghai` timestamps from the
central clock helpers: `shanghai_now`, `shanghai_today`, and
`to_shanghai_aware`. The central `Quote` contract rejects naive
`observed_at`, `fetched_at`, and evaluation timestamps. UTC and other aware
timezones are accepted and converted by instant; the contract must never use
`replace(tzinfo=...)` to guess provider semantics.

Database `DateTime` columns retain two explicit historical storage contracts:

- `market.*` quality and cache timestamps are naive Shanghai wall time.
- `fundamental.profile`, `announcement.catalog`, and other non-market quality
  timestamps are naive UTC.

Persistence and restoration must use the centralized capability-specific
conversion helpers. A naive datetime is accepted only at an explicitly
declared storage or legacy compatibility boundary. Effective quality converts
stored values according to capability before freshness, supersession, and
future-time comparisons; it must not interpret all historical naive values as
Shanghai time.

`observed_at` is the business time represented by the quote or bar;
`fetched_at` is when this system completed the request. A missing business
timestamp is never replaced with `fetched_at`, and a `fetched_at` later than
the evaluation clock beyond the contract tolerance is rejected.

A-share continuous sessions are half-open intervals: `[09:30, 11:30)` and
`[13:00, 15:00)`. Therefore 11:30 is lunch break and 15:00 is closed. The
official daily close is always 15:00; any later data-landing buffer is a
separate operational concern.

`market.quote.realtime` accepts only a current-session realtime `Quote` with
matching symbol, CNY units, explicit timestamps, and an observation inside the
current continuous window. `market.quote.latest_close` accepts only an
official 15:00 close for a completed exchange session and never satisfies a
realtime request.

`market.daily.qfq` and `market.daily.unadjusted` require explicit adjustment
and units on every `DailyBar`, matching symbols, unique increasing trade dates,
legal OHLC values, and non-negative volume. Every trade date must be an
exchange session no later than `latest_completed_session(evaluated_at)`, and
`observed_at` must equal that session's official 15:00 Shanghai close. A
current-day row is rejected before 15:00, including during lunch, and becomes
eligible only once the session is complete. Rows in one response must also
share source, timezone semantics, and effectively identical fetch time.
Router validation occurs before a `QualityObservation` is formed; persistence
preflight remains the second boundary.

The public AKShare adapter deterministically removes rows later than the latest
completed session and stamps retained bars at official close. Professional
adapters reject incomplete rows and naive response timestamps. Both approaches
produce the same downstream daily-bar contract.

- Daily market capabilities use the A-share trading calendar and trading-session lag, not
  natural-day subtraction.
- Announcement catalog freshness uses the successful scan `checked_at`. The latest
  announcement publication date is business content and does not determine scan freshness.
- A successful empty announcement scan is fresh Evidence with `row_count=0`; a failed or absent
  scan is `MISSING`.

Preview `generated_at` and all new `DecisionPackage` `created_at`,
`generated_at`, and `expires_at` values are aware Shanghai timestamps. Package
expiry is exactly 24 hours after package generation and is the authoritative
confirmation-age gate. `AnalysisRun.created_at` is explicitly stored as naive
Shanghai wall time, but it is only a secondary compatibility check for legacy
packages with naive generation timestamps. Host timezone must not influence
preview hashes, package hashes, expiry, or confirmation.

One-click analysis captures one aware Shanghai `analysis_started_at`. The announcement
catalog window, refresh scheduling, selector lookup, Evidence timestamps, preview timestamp,
and package timestamp all receive that same instant. The canonical announcement window ends on
the Shanghai business date and starts exactly `lookback_days` earlier; Subject construction,
Router calls, persistence, selection, and source binding reuse the same dates even if execution
crosses Shanghai midnight.

Research persistence remains UTC naive. Public Research results and formal Evidence are
converted to aware timestamps before serialization. Announcement `scan_start` and `scan_end`
represent the exact requested date coverage and must never be replaced by request completion
time. Host-local date and time functions cannot define an announcement scope or authoritative
Research timestamp.

Announcement URLs are normalized only after Router quality and digest auditing. Persistence
stores a full, collision-free ASCII URL while retaining the original Provider value in
`raw_data`; MySQL uses `VARCHAR(1000) CHARACTER SET ascii COLLATE ascii_bin` so the complete
`(symbol, url)` uniqueness constraint remains within InnoDB's index limit without prefixing.
MySQL authority timestamps use `DATETIME(6)` to preserve exact lineage precision; market values
remain Shanghai wall-time naive and Research values remain UTC naive, with no global time shift.

If freshness expires after analysis, freeze must reject the package and require a new analysis.

## 5. New conflicts and old cache

A new `CONFLICTED` refresh never destroys a previously trusted cache row, but it immediately
removes that cache row's execution eligibility for the affected capability and subject.
The conflict is persisted as a newer audit decision with its provider observations and fields.

Analysis and freeze must consider blocking audit records created after the trusted cache and
after package generation. A caller cannot bypass a newer conflict by loading only the last
successful business row. Resolving the conflict requires a later trusted refresh with explicit
lineage; merely rereading or rewriting the old cache is not resolution.

## 6. Analysis and freeze consistency

All entry points must call the same policy and freshness functions. UI labels and API metadata
must describe the same effective result used by `DecisionPackage` and freeze.

Analysis must:

1. Build Evidence for every required capability.
2. Compute the quality snapshot and execution gate from that Evidence.
3. Keep deterministic `rule_status` separate from `executable_status`.
4. Set `ready_allowed` and `freeze_allowed` to false for blocking quality.

Freeze must:

1. Validate the complete `DecisionPackage`, hashes, required Evidence, and expiry.
2. Recompute freshness using current time and the same capability policies.
3. reject required data or blocking audit changes after package generation.
4. Recompute the deterministic preview and compare rule/account/preview snapshots.
5. Persist through the unified freeze service within one transaction.

No API, scheduler, background task, legacy save endpoint, or frontend action may write a formal
plan around this service. One analysis run and one `(account_id, symbol, plan_version)` tuple
remain database-enforced uniqueness boundaries.

## 7. Required and optional Evidence

Required Evidence participates in aggregate quality and controls `READY` and freeze eligibility.
Every required capability must be represented, even when its value is missing; absence is
recorded as `MISSING`, not omitted.

Optional Evidence may enrich research and may be missing without independently blocking a plan.
It must still carry its own quality and lineage, and the UI must not present it as verified.
Promoting a capability from optional to required is a policy change that needs end-to-end
regression coverage.

All AI factual claims must reference valid `evidence_id` values in the package. Compatibility
aliases such as `source_ids` must resolve to the same validated IDs and must not bypass nested
claim validation. AI output is advisory and cannot modify deterministic rule state, buy zones,
positions, quantities, or hard stops.

## 8. End-to-end test matrix

Tests must traverse production connections rather than fabricating the terminal state.

| Scenario | Provider/Data Hub | Persistence/cache | Analysis/package | Freeze/confirm | Expected |
|---|---|---|---|---|---|
| Verified sources agree | Two canonical matches | Trusted lineage retained | Required Evidence `VERIFIED` | Allowed if rules pass | Executable |
| One fresh source | One valid observation | `SINGLE_SOURCE` retained | Required Evidence present | Allowed by Phase 1 policy | Executable |
| Sources conflict | Canonical digests differ | Audit only; trusted row not overwritten | Blocking Evidence | Rejected | No formal plan |
| Trusted cache plus new conflict | Refresh conflicts | Old row retained; new conflict recorded | New conflict wins eligibility | Rejected | No cache laundering |
| Stale cache | Provider unavailable | Stored status plus current freshness | `STALE` | Rejected | No formal plan |
| Missing data | No value and no cache | Missing audit state | Required Evidence `MISSING` | Rejected | No formal plan |
| Empty announcement catalog | Successful zero-row scan | Scan state persisted | Required fresh Evidence | Allowed if other gates pass | Distinct from missing |
| Quote stale, bars fresh | Realtime quote expires | Independent quote/bar lineage | Price-triggered action blocked | Rejected when required | Bars do not mask quote |
| Morning/lunch current-day bar | Provider returns partial current session | Rejected before cache replacement | No partial bar Evidence | Rejected | Prior complete session remains authoritative |
| After-close current-day bar | Provider returns official-close row | Persisted with 15:00 observation | Completed-session Evidence | Allowed if other gates pass | Current session is usable |
| Future fetch timestamp | Provider clock is ahead | Rejected before persistence | `MISSING` attempt | Rejected | Existing trusted cache is unchanged |
| UTC 02:00 confirmation | Aware UTC instant converts to 10:00 Shanghai | Existing realtime lineage revalidated | Same package | Allowed if otherwise valid | Host timezone independent |
| UTC 07:00 confirmation | Aware UTC instant converts to 15:00 Shanghai | Realtime quote recomputes stale | Package becomes non-executable | Rejected | Close boundary enforced |
| Freshness expires after analysis | Initially trusted | No rewrite required | Package becomes stale | Rejected on recomputation | Re-analyze |
| AI invalid reference/mutation | Valid base Evidence | No deterministic writes | Validation rejects AI output | Deterministic package unchanged | AI isolated |
| Duplicate confirm | Same run, independent Sessions | Unique `analysis_run_id` | Same package | One success, stable conflict/idempotence | One plan |
| Version race | Same account/symbol/version | Database unique constraint | Independent Sessions | At most one insert | No duplicate version |

The matrix must run on SQLite and, in CI, an isolated MySQL 8 database. Provider tests must not
access real external networks. Tests must not monkeypatch the final pipeline step, fabricate
`DecisionPackage`, or assign a final quality status as proof that the production path works.
