# FreeStockDB Native Adapter Contract

This adapter targets the bounded native root-command protocol verified against the local
FreeStockDB v0.2.1 service at `http://127.0.0.1:7899`. It does not use the earlier fixture-only
`/health`, `/api/v1/history/daily`, or `schema_version=1.0` contract.

## Fixed Requests

The client exposes no arbitrary command, table, URL, SQL, shell, updater, binary, or local
database entry point. It builds only these requests:

- Daily rows: `GET /?cmd=vals&t=日k&k1=key:<symbol>&k2=fwd:<YYYYMMDD>,<YYYYMMDD>`
- Adjustment factors: `GET /?cmd=get&t=复权&k1=key:<symbol>&k2=all:`

The daily response root is `list[dict]`, normally newest first. Required native fields are
`date`, `code`, OHLC, `pre_close`, `volume`, `amount`, and `turnover`. The adapter maps `date` to
`trade_date` and `turnover` to `turnover_rate`, validates symbol and date scope, trading
sessions, monotonic unique dates, finite numbers, and OHLC relationships, then returns rows in
ascending trade-date order. Missing `amount` or `turnover` makes the capability unavailable;
neither value is inferred or filled.

The factor response root is `list[["复权:<symbol>:<YYYYMMDD>", {"cum": value}]]`. QFQ uses
`ratio = latest_cum / current_cum` and divides only OHLC and `pre_close` by that ratio. Volume,
amount, and turnover are unchanged. The latest factor therefore has ratio 1.

## Capabilities and Units

The Provider advertises only:

- `market.daily.qfq`
- `market.turnover.daily`

Native field semantics are declared as volume=`share`, amount=`CNY`, and turnover=`percent`.
The local protocol returned populated values for these fields, but trusted cross-source ratios
could not be completed because the existing AKShare source was unreachable. This limitation
must remain visible in release reporting.

No documented, verified HTTP command for CSI300 history was found in the local release package.
Therefore the Provider does not advertise `market.index_daily`, and direct index requests fail
with `CSI300_HTTP_CAPABILITY_UNAVAILABLE`. History Bootstrap must remain `BLOCKED` with
`BENCHMARK_HISTORY_MISSING`; it must not fabricate index data or silently fall back to AKShare.

## Security, Health, and Lineage

Only loopback origins are enabled by default. Redirects are rejected, timeouts and retries are
bounded, response bytes are capped, non-JSON or non-list roots are rejected, and external error
text is truncated. Health uses one fixed daily-row canary rather than `/health` and distinguishes
`DISABLED`, `UNREACHABLE`, `PROTOCOL_UNSUPPORTED`, `DATA_UNAVAILABLE`, `READY`, and `DEGRADED`.

QFQ lineage binds both native requests and responses: daily and factor request digests, daily
and factor response digests, combined raw digest, adapter and protocol versions, requested
start/end and adjustment, actual fields, row count, schema fingerprint, and query-free source
URL. Router audit and atomic persistence retain this exact lineage. Results remain capped at
`SINGLE_SOURCE` quality.
