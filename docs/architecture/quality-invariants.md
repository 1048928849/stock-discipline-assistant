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
- Daily market capabilities use the A-share trading calendar and trading-session lag, not
  natural-day subtraction.
- Announcement catalog freshness uses the successful scan `checked_at`. The latest
  announcement publication date is business content and does not determine scan freshness.
- A successful empty announcement scan is fresh Evidence with `row_count=0`; a failed or absent
  scan is `MISSING`.

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
| Freshness expires after analysis | Initially trusted | No rewrite required | Package becomes stale | Rejected on recomputation | Re-analyze |
| AI invalid reference/mutation | Valid base Evidence | No deterministic writes | Validation rejects AI output | Deterministic package unchanged | AI isolated |
| Duplicate confirm | Same run, independent Sessions | Unique `analysis_run_id` | Same package | One success, stable conflict/idempotence | One plan |
| Version race | Same account/symbol/version | Database unique constraint | Independent Sessions | At most one insert | No duplicate version |

The matrix must run on SQLite and, in CI, an isolated MySQL 8 database. Provider tests must not
access real external networks. Tests must not monkeypatch the final pipeline step, fabricate
`DecisionPackage`, or assign a final quality status as proof that the production path works.
