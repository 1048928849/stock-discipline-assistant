# P1S-M2-R1 real-industry verification — 2026-08-09

## Attempt boundary

- Provider: AKShare `1.18.81`, Eastmoney industry-board APIs.
- Capability attempted: `industry.membership.native` discovery prerequisite.
- Local time zone: Asia/Shanghai.
- Retries: 1 for the bounded probe.
- Result: the Eastmoney industry-universe request failed through the configured proxy before
  a provider taxonomy or constituent universe could be obtained.
- Root exception: `ProxyError` for `17.push2.eastmoney.com`.
- Formal A-share blocker: `EXTERNAL_INDUSTRY_DATA_BLOCKED`.
- Formal BSE blocker: `BSE_INDUSTRY_SOURCE_UNAVAILABLE`.

Missing values below remain missing. They are not converted to zero, neutral, PASS, or a role.

## Symbol results

| Symbol | Profile industry | Provider taxonomy / ID / name | Membership | Industry rows / latest date | Constituents / ready | History / amount coverage | 20d / 60d / amount rank | Role | Cycle / plan | Entry / stop | Initial / max position | Degradation |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 300308 | not used as provider ID | unavailable | not verified | missing | missing | missing | missing | UNKNOWN | not evaluated | missing | 0 / 0 | EXTERNAL_INDUSTRY_DATA_BLOCKED |
| 300502 | not used as provider ID | unavailable | not verified | missing | missing | missing | missing | UNKNOWN | not evaluated | missing | 0 / 0 | EXTERNAL_INDUSTRY_DATA_BLOCKED |
| 000938 | not used as provider ID | unavailable | not verified | missing | missing | missing | missing | UNKNOWN | not evaluated | missing | 0 / 0 | EXTERNAL_INDUSTRY_DATA_BLOCKED |
| 600519 | not used as provider ID | unavailable | not verified | missing | missing | missing | missing | UNKNOWN | not evaluated | missing | 0 / 0 | EXTERNAL_INDUSTRY_DATA_BLOCKED |
| 920985 | not used as provider ID | unavailable | not verified | missing | missing | missing | missing | UNKNOWN | not evaluated | missing | 0 / 0 | BSE_INDUSTRY_SOURCE_UNAVAILABLE |

## Exit-gate interpretation

The bounded real-provider integration was attempted, but the required native taxonomy schema
and constituent membership could not be observed. Therefore the target of three verified
A-share memberships and two 80% member-evidence universes is not claimed. Deterministic fixture
coverage and browser release smoke may pass, while real industry role evidence remains blocked.
