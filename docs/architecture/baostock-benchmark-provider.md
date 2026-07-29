# BaoStock Benchmark Provider Contract

BaoStock is an independent, single-purpose source for `market.index_daily`. The adapter pins
`baostock==0.9.3` (package-reported version `00.9.30`, BSD license) and maps only internal symbol
`CSI000300` to BaoStock code `sh.000300`. It requests fixed fields `date`, `code`, OHLC,
`preclose`, `volume`, `amount`, and `pctChg` with `frequency=d` and `adjustflag=3`.

FreeStockDB remains responsible for stock QFQ history and daily amount/turnover. BaoStock is not
a FreeStockDB fallback and does not provide stock, quote, sector, financial, minute, or realtime
capabilities. The capability routes are:

- `market.daily.qfq` and `market.turnover.daily` -> FreeStockDB
- `market.index_daily` -> BaoStock benchmark Provider

Both sources are single-source inputs, so their maximum quality is `SINGLE_SOURCE`.

## Process Boundary

BaoStock's native 0.9.3 SDK uses blocking sockets and exposes no effective socket timeout or
cancellation API. The web process never imports or calls its login/query/logout functions.
Instead, the parent launches the fixed command
`[sys.executable, "-m", "app.providers.baostock_worker"]` with `shell=False` and a single JSON
request on stdin. The worker supports only the fixed CSI300 daily operation, logs in, reads the
bounded result, logs out in `finally`, and emits one JSON object on stdout.

The parent enforces a 1-60 second wall-clock timeout with `Popen.communicate(timeout=...)`. On
timeout it kills the worker and calls `communicate`/`wait` to reap it before raising
`BaoStockTimeoutError`. Response bytes and rows are capped; stderr and external error text are
truncated. The default configuration is disabled, uses a 10-second timeout, limits stdout to
2,000,000 bytes, and permits at most 500 rows. Default CI never calls the BaoStock network;
real tests require `RUN_BAOSTOCK_INTEGRATION=1`.

## Validation and Lineage

The Provider rejects missing or extra protocol fields, invalid JSON, nonzero worker exits,
wrong codes, empty/non-finite numbers, invalid OHLC, negative volume/amount, duplicate or
unordered dates, out-of-range rows, fewer than 80 sessions, stale end dates, partial windows,
and digest or row-count mismatches. Prices and quantities are parsed explicitly as `Decimal`.
The declared units are price=`CNY`, volume=`share`, and amount=`CNY`; adjustment is
`unadjusted`.

Lineage records provider and adapter IDs, worker and package versions, internal/external symbols,
requested range and fields, actual fields, units and adjustment, row count, request/response
digests, schema fingerprint, aware fetch time, process isolation mode, and timeout. It excludes
raw responses and environment variables. Router audit, exact-result persistence validation, and
atomic cache replacement remain mandatory before `mark_persisted`.

Five common 600519 sessions showed FreeStockDB/BaoStock volume ratios of `0.9999862` to
`1.0000091`, amount ratios of `0.9999994` to `1.0000010`, and turnover ratios of `0.9952083` to
`1.0160219`. The unit semantics agree; turnover differs slightly because of source precision and
rounding and is not asserted to be identical.

If BaoStock is disabled, unreachable, timed out, stale, or invalid and no executable benchmark
cache satisfies the normal quality resolver, Bootstrap must remain blocked with an explicit
benchmark reason. It may not silently reuse stale data, substitute an ETF such as 510300, or
synthesize CSI300 history.
