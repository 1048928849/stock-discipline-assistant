# P1S-M2-R1 real-industry verification — 2026-08-09

## Attempt boundary

- Provider: AKShare `1.18.81`, Eastmoney industry-board APIs.
- Capability attempted: `industry.membership.native` discovery prerequisite.
- Request type: HTTPS `GET` of the Eastmoney industry-board universe, followed only on
  success by native constituent and board-history requests.
- Local time zone: Asia/Shanghai.
- Outer timeout: 20 seconds. AKShare's request returned after 13.643 seconds, so the outer
  timeout did not fire.
- Retries: one bounded application probe. AKShare's internal request helper exhausted its
  own bounded retry sequence; no application-level retry was added.
- DNS: successful for `17.push2.eastmoney.com` (`43.144.251.121`, 0.012 seconds).
- Direct TCP: successful on port 443 (0.037 seconds).
- Direct TLS: successful with TLS 1.2 (0.130 seconds).
- HTTP control request: a one-row Eastmoney request returned HTTP 200 on one direct route;
  another route followed `17.push2delay.eastmoney.com` and ended with an unexpected TLS EOF.
- Actual provider result: AKShare's industry-universe request failed before a usable HTTP
  response or provider taxonomy was obtained. The root exception was
  `requests.exceptions.ProxyError` for `17.push2.eastmoney.com`, caused by
  `RemoteDisconnected("Remote end closed connection without response")` while connecting
  through AKShare's request path.
- Failure stage: not DNS, TCP, or the initial direct TLS handshake. It is the HTTP request
  path used by AKShare (proxy/remote disconnect before an HTTP status was available).
- Subprocess lifecycle: the provider probe ran in a child process, returned exit code 1, and
  was reaped normally. No orphan remained.
- Formal A-share blocker: `EXTERNAL_INDUSTRY_DATA_BLOCKED`.
- Formal BSE blocker: `BSE_INDUSTRY_SOURCE_UNAVAILABLE`.

Missing values below remain missing. They are not converted to zero, neutral, PASS, or a role.

## Alternative-provider check

A separate, bounded Baostock probe logged in successfully and returned one record for
`sz.300308`: `C39计算机、通信和其他电子设备制造业` under `证监会行业分类`. It returned no
record for `bj.920985`; logout completed, the child exited with code 0 in 1.486 seconds, and
the subprocess was reaped normally.

Baostock is therefore reachable but is not a verified substitute for this capability. Its
record is a per-security CSRC classification, not the Eastmoney native board taxonomy used
by the selected-stock industry's breadth series. The probe did not provide the matching
native industry ID, exact constituent universe, board history, or amount-coverage evidence
required by the schema contract. Mixing that classification with Eastmoney board history
would violate the same-taxonomy/provider rule. For `920985`, the empty result supplies no BSE
membership evidence at all. No other provider was validated with all of those contracts.

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
