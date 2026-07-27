# FreeStockDB Adapter Contract

This document records the fixed `1.0` fixture contract implemented by this repository. The
service at `http://127.0.0.1:7899` was not reachable during implementation, so this is not a
claim about a verified `hello245m/free-stockdb` production protocol. A real integration run must
confirm the endpoint paths, CSI300 symbol, units, adjustment semantics, and schema before the
provider is enabled.

## Fixed requests

- `GET /health` returns an object with `schema_version=1.0`, `service=free-stockdb`,
  `status=ok`, and an optional version.
- `GET /api/v1/history/daily` accepts only adapter-built `kind`, `symbol`, `start`, `end`,
  `adjustment`, and fixed `fields` query parameters.
- The history response root is `{schema_version, data}`. `data` contains `symbol`,
  `adjustment`, `price_unit=CNY`, `volume_unit=share`, `amount_unit=CNY`,
  `turnover_rate_unit=percent`, and ordered `rows`.
- Each row contains `trade_date`, OHLC, `volume`, native `amount`, and native
  `turnover_rate`. Missing native turnover data makes `market.turnover.daily` unavailable; it is
  never inferred.

The configured `FREESTOCKDB_CSI300_SYMBOL` is intentionally empty by default because the real
external symbol mapping has not been confirmed.

## Security and lineage

The client has no arbitrary command, URL, SQL, shell, updater, binary, or local data-directory
entry point. Remote origins are disabled by default, redirects are not followed, responses are
read with a byte limit, retries and timeouts are bounded, and external error text is truncated.
Router audit retains the adapter version, requested scope and fields, row count, schema and raw
response digests, and a query-free fixed endpoint URL. Single-source results remain
`SINGLE_SOURCE`.
