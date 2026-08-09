# P1T-M1 — Trading Discipline Coach

This module evaluates decision-process quality. It is not a price predictor or trading
signal engine. A PASS means only that the discipline layer did not block the proposed
action; Product V1 and upstream hard gates retain formal authority.

## Authority

- P1T can block BUY/ADD, reduce a position cap, or require re-analysis.
- P1T cannot create `ENTRY_ALLOWED`, loosen a hard stop, raise Product V1 sizing,
  override `NO_TRADE`, make CSV_V2 executable, freeze a plan, or place an order.
- FACT may update the factual evidence set after validation. ANALYSIS may update a
  hypothesis. SENTIMENT is context only. None has formal execution authority by itself.
- AI text cannot calculate the score or change any gate, stop, quantity, or historical
  result.

## Deterministic model

The active training playbook is `CORE_STATE_CHANGE_V1` version `1.0.0`: state change,
second confirmation, then the first effective divergence. `MarketStage` is persisted
separately from `WatchlistStatus`.

State-change detection requires independent structure, price, volume, candle-close,
relative-strength, or industry evidence groups. A single percentage rise is insufficient.
Subsequent confirmation uses only sessions after the original signal. Acceleration and
climax produce chase/late-confidence constraints, not loss predictions.

Pre-trade rules return `PASS`, `WARN`, `BLOCK`, or `NOT_EVALUATED`, with required,
evaluated, and missing inputs, thresholds, actual values, evidence, source references,
reason codes, and action effects. Any BLOCK remains binding regardless of score.

## Score and replay

The immutable 100-point score contains five 20-point categories:

1. predefined playbook;
2. pre-existing entry condition;
3. predefined invalidation and hard stop;
4. predefined position;
5. rule stability.

P&L, MFE, and MAE are later review outcomes and cannot change the historical score.
Evidence queries for a historical decision exclude items published or observed after that
decision. Thesis changes create linked revisions; the original hash-bound snapshot remains.

## Position stress and training

Position stress uses account value, planned/current quantity, single-name open risk,
sector/total exposure, cash, -3%/-5%/-10% adverse impacts, hard-stop impact, and T+1 gap
risk. CRITICAL blocks expansion while REDUCE/EXIT remain available.

The first training program targets 20 valid samples. Fewer samples cannot produce a
playbook-quality conclusion, and 20 samples are not proof of profitability. Reviews keep
outcome and compliance independent, including `PROFITABLE_UNDISCIPLINED` and
`LOSING_COMPLIANT`.

## Order-flow boundary

`BuyImpactObservation`, `PriceImpactEfficiency`, and divergence/weakening contracts exist
for future trustworthy tick/minute/L2 activation. Daily-only or unverified observations
return `INSUFFICIENT_DATA`; a large print never implies accumulation, washing, distribution,
or bullish intent.

## API and UI

APIs under `/api/v1/trading-discipline` cover playbooks, training programs, pre-trade
checks, immutable theses, evidence, reviews, and dashboard metrics. The “交易训练 / 纪律”
page presents N/20 progress, process score, recurring errors, independent P&L/compliance
labels, and a pre-trade decision sheet on desktop and mobile.

## A-share decision guardrails 1.0

The end-to-end decision card evaluates exactly seven ordered gates:
`INFORMATION → CHANGE → HIERARCHY → STAGE → PRICE_BEHAVIOR →
RISK_INVALIDATION → POSITION`.

Its first-class actions are `OBSERVE`, `WAIT_FOR_CONFIRMATION`,
`EXECUTION_CANDIDATE`, and `ABANDON`. Observation is productive: it persists the evidence
already seen, evidence still required, invalidation, and next reassessment trigger.
`EXECUTION_CANDIDATE` remains non-executable and must still pass Product V1 authority.

Hard vetoes cannot be offset by the 100-point score: `RESEARCH_STARTED_AFTER_SPIKE`,
`LOW_AUTHORITY_PRIMARY_REASON`, `MARKET_STAGE_UNRESOLVED`, `MISSING_INVALIDATION`,
`UPSIDE_FIRST_DECISION_PROCESS`, `POST_WIN_RISK_ESCALATION`,
`LOSS_RECOVERY_TRADE_RISK`, and `DECISION_CONTEXT_CONFLICTED`. Observable
`TRADE_HORIZON_DRIFT` is also blocked: a failed short-term thesis cannot silently become a
long-term thesis. A genuinely new investment thesis requires a new linked immutable snapshot.

Price behavior compares expected with actual behavior and returns
`STRONGER_THAN_EXPECTED`, `AS_EXPECTED`, `WEAKER_THAN_EXPECTED`, or `NOT_EVALUATED`.
It never substitutes a narrative about “main-force intention.” Account DecisionContext uses
only observable recent win/loss, re-entry timing, risk escalation, and conflicting-information
counts; it does not diagnose psychology.
