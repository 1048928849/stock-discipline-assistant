# Recent-window market breadth provider

`market-breadth-eod/1.0.0` owns only `market.breadth.daily`. It combines fields
from two sources without treating them as independent verification of the same
business value:

- FreeStockDB supplies one bounded `日k` cross-section request with fixed native
  parameters: `cmd=vals`, `k1=all:`, and `k2=key:YYYYMMDD`.
- AKShare supplies the official recent-window limit-up and limit-down pools through
  the process-isolated `limit_pools` worker.

The successful daily path uses three source requests. The AKShare worker accepts one
fixed JSON request, emits one bounded JSON response, runs with `shell=False`, and is
killed and reaped after its hard timeout. Arbitrary modules, functions, URLs, and
commands are not accepted.

## Universe and failure policy

The normalized universe includes recognized Shanghai, Shenzhen, and Beijing A-share
code ranges. B shares, funds, ETFs, bonds, indices, invalid prices, and rows without
positive volume are excluded. ST securities remain included. Prices and percentage
changes use `Decimal`, and normalized rows are sorted by symbol before hashing.

Official pool symbols are intersected with the valid universe. Known non-target
instruments are recorded and excluded. A target A-share missing a valid daily row
fails with `BREADTH_UNIVERSE_INCOMPLETE`; unknown or unnormalizable symbols fail with
`BREADTH_POOL_UNIVERSE_MISMATCH`. The provider never substitutes a 9.8 percent rule,
zero, a broken-limit pool, or a spot snapshot.

AKShare's down-limit pool is limited to the recent window. Requests older than 30
calendar days fail with `BREADTH_LIMIT_EVIDENCE_UNAVAILABLE`. Therefore this provider
supports latest-session EOD capture and bounded recent backfill, not arbitrary
historical reconstruction.

## Persistence and scheduling

The capture service uses the normal Router audit and product persistence path. A
successful result is persisted atomically and then marked persisted; failed refreshes
leave the previous cache untouched. Same-date captures reuse an existing trusted row
unless an explicit internal force refresh is requested.

The existing scheduler can run one post-close capture when
`MARKET_BREADTH_ENABLED=true`. A database lease prevents overlapping executions.
The setting defaults to disabled, and no online request automatically backfills
multiple dates.

## Current external limitation

The July 29, 2026 acceptance probe found official pool symbols `301583` on July 24
and `920176` on July 28 absent from the corresponding FreeStockDB cross-sections.
The strict provider correctly blocks both captures. Until upstream universe coverage
is complete, a real executable `MarketRegimeSnapshot` cannot be claimed from this
source combination, and P1D remains blocked.
