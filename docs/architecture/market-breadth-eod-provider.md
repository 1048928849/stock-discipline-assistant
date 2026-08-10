# Canonical market breadth provider

`market-breadth-eod/1.0.0` owns `market.breadth.daily`. Its universe is the
versioned `A_SHARE_SH_SZ/1.0.0` contract: Shanghai main board and STAR Market,
Shenzhen main board and ChiNext, common stocks only. BSE, B shares, ETFs, funds,
bonds and indices are outside this scope. The product must call this universe
“沪深普通A股”, never “全A市场”.

BaoStock security master evidence derives the date-specific membership snapshot.
Listing and delisting dates decide historical membership; exchange and board are
classified from normalized codes, never display names. The membership digest is
independent of fetch time.

## Reconciliation

FreeStockDB is the primary exact-date price cross-section. Every listed member must
have a valid close and previous close. A bounded public-history adapter may fill a
small primary-provider gap only when symbol, date, units and adjustment semantics
are validated. It records `BREADTH_PRIMARY_PROVIDER_GAP_SUPPLEMENTED`; it is not an
independent vote and cannot conceal an unresolved member.

AKShare provides raw limit-up/down pools through a bounded, process-isolated worker.
Raw pools are projected onto the same membership snapshot. Outside-scope symbols,
including BSE securities, remain visible in reconciliation lineage but are not
counted as missing SH/SZ members. An in-scope pool member without price evidence
blocks persistence.

Only `COMPLETE` and `COMPLETE_WITH_SUPPLEMENTARY_DATA` are persistable. `INCOMPLETE`
and `UNIVERSE_UNAVAILABLE` cannot create neutral market state, increase risk, or
produce a persisted market regime. Suspensions are excluded only with explicit
suspension evidence; no zero return is invented.

## Persistence and identity

Every breadth snapshot binds universe ID/version, membership and reconciliation
digests, primary/supplementary counts, completeness and source lineage. Its unique
identity includes the universe version and trade date. Fetch timestamps do not
change business identity, while changed membership or universe policy does.

The capability subject is `market/CN-A/daily/a-share-sh-sz`. Product V1 exposes the
universe name, covered exchanges/boards, as-of date, completeness, source counts and
degradation state beside breadth. Technical symbol-level reconciliation stays in
collapsed evidence/lineage.

## Bounded execution

The security-master and limit-pool adapters use fixed subprocess protocols,
`shell=False`, output limits, hard timeouts and kill/reap cleanup. FreeStockDB is
restricted to loopback. Supplementation is capped by
`MARKET_BREADTH_MAX_SUPPLEMENT_SYMBOLS` and never expands into an unbounded crawl.

The provider supports recent-session EOD capture. Missing local FreeStockDB service
is an external-data limitation and is reported as unavailable, not replaced with a
spot snapshot or fabricated breadth.
