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
