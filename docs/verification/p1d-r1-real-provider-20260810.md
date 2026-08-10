# P1D-R1 bounded real-provider verification — 2026-08-10

## Scope and result

The bounded probes covered 2026-07-24, 2026-07-28 and the latest completed session
used by this program, 2026-08-07. No probe persisted data.

BaoStock security-master calls succeeded for all three dates. After filtering its
8,885 instrument records to type-1 common stocks, the provider derived 5,200,
5,201 and 5,205 listed SH/SZ members respectively. `301583` is a listed ChiNext
common stock on the two July dates. `920176` is absent because BSE is intentionally
outside `A_SHARE_SH_SZ/1.0.0`, not because of an SH/SZ membership gap.

AKShare official limit-pool subprocess calls also succeeded and exited normally:

| Date | Raw limit-up | Raw limit-down | Duration |
|---|---:|---:|---:|
| 2026-07-24 | 40 | 25 | 4.031 s |
| 2026-07-28 | 61 | 49 | 4.005 s |
| 2026-08-07 | 74 | 4 | 3.917 s |

The bounded public-history supplement for `301583` succeeded. Tencent qfq evidence
contained 2026-07-23 close 145.00 and exact-date 2026-07-24 close 174.00, so this
known primary gap is supplementable with explicit lineage.

## External limitation

The complete provider was attempted once per date with zero retries, a two-second
HTTP timeout and a 15-second total budget. Each call stopped in 0.21–0.23 seconds at
the primary cross-section stage. The configured endpoint was
`http://127.0.0.1:7899`; DNS was not involved, TCP connect was refused, and no
TLS/HTTP response stage was reached. A separate bounded curl probe confirmed error
7 / connection refused. There was no child process to reap at that stage.

Consequently no real reconciled breadth or `MarketRegimeSnapshot` is claimed. The
membership provider, pool provider and targeted supplementary provider were
independently validated, but none supplies the full primary cross-section contract.
The system therefore returns unavailable and preserves strict non-persistence.

Classification: `PASS_WITH_EXTERNAL_DATA_LIMITATION` is permitted after all local,
migration, contract and regression gates pass.
