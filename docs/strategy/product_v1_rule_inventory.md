# Product V1 Rule Inventory

## Scope and authority

Product V1 remains the only formal execution strategy. The CSV_V2 selected-stock strategy is
registered as disabled in the application registry and is resolved only by the advisory service.
It cannot enter a DecisionPackage, confirm, freeze, or grant execution permission.

Formal Product V1 chain:

`one-click request -> RequiredDataPlan -> DataHub Router -> exact persistence ->
ProductAnalysisSnapshot -> enabled StrategyRegistry entries -> Global Risk Constraint ->
DecisionPackage -> confirm -> unified freeze`

## Current rule inventory

| Rule ID | Inputs | Output | Default / authority | Hard gate |
|---|---|---|---|---|
| PV1-DATA-001 | required capability, Subject, quality record | executable capability | effective-quality resolver | yes |
| PV1-DATA-002 | quality status | block / continue | CONFLICTED, STALE and MISSING block | yes |
| PV1-DATA-003 | ProviderResult and prepared rows | persisted cache | exact digest, row count and lineage | yes |
| PV1-MKT-001 | breadth, amount, CSI300, previous persisted state | market regime | deterministic market engine | yes when required |
| PV1-IND-001 | full industry universe and constituents | industry classification | deterministic industry engine | yes when required |
| PV1-TECH-001 | qfq daily history | technical snapshot | deterministic technical engine | yes when required |
| PV1-ENTRY-001 | support, ATR and rule parameters | buy zone | Product V1 entry engine | yes |
| PV1-ENTRY-002 | current price and buy zone | trigger assessment | latest-close does not imitate realtime | yes |
| PV1-RISK-001 | buy zone and hard stop | per-share risk | Product V1 risk engine | yes |
| PV1-RISK-002 | capital and risk budget | final and trial quantity | A-share lot and portfolio limits | yes |
| PV1-RISK-003 | account, industry and total exposure | allowed quantity | Global Risk has final authority | yes |
| PV1-EXIT-001 | hard stop and invalidation | exit plan | deterministic exit engine | yes |
| PV1-PKG-001 | snapshot, evidence and strategy bindings | package hash | canonical deterministic hash | yes |
| PV1-CONFIRM-001 | package evidence and snapshot bindings | confirm permission | revalidate current authority | yes |
| PV1-FREEZE-001 | strategy/version/hash and quality bindings | immutable plan | unified freeze service | yes |
| PV1-AI-001 | structured result and evidence | explanation only | optional LLM | no execution authority |

## Inputs and outputs

The formal inputs include symbol, account/capital state, current holding state, explicit risk
parameters, a single captured `analysis_started_at`, and exact capability snapshots. Formal
outputs include rule status, market and industry assessments, buy plan, position calculation,
exit plan, evidence, quality bindings, StrategyBinding, package hash, and freeze result.

Default risk parameters remain owned by Product V1 configuration and request schemas. CSV_V2
does not overwrite them. Account, cash, holding quantity, holding cost, exposure ceilings and
quality state are real inputs; AI text is never an input to price or quantity calculations.

## Fixed calculations and compatibility contract

The accepted golden result remains unchanged:

| Field | Fixed result |
|---|---:|
| `rule_status` | `READY` |
| buy zone | `[10.4209, 10.5391]` |
| hard stop | `9.7023` |
| final quantity | `600` |
| trial quantity | `100` |
| per-share risk | `0.7777` |
| maximum loss | `77.77` |

The buy zone is calculated by the existing entry engine from its accepted support/volatility
inputs. Position size is calculated by the existing risk engine from capital, per-trade risk,
stop distance, A-share lot size and portfolio ceilings. Stop and take-profit behavior remains in
the existing deterministic exit and trade-plan implementation. This milestone does not copy or
replace those algorithms.

## Confirm and freeze revalidation

Confirm/freeze continues to verify the DecisionPackage hash, Evidence bindings,
ProductAnalysisSnapshot hash, StrategyBinding strategy ID/version, implementation hash,
parameter hash, signal hash, data quality and expiry. A disabled strategy cannot validate an old
formal package. CSV_V2 never creates a formal StrategyBinding, so it cannot be confirmed or
frozen.

## AI boundary

AI may summarize evidence, explain passed/failed rules, describe risks and compare strategies.
It cannot calculate or alter indicators, rule status, buy zone, stop, position, quantity,
plan status, hard gates, DecisionPackage, confirm, freeze, or execution permission. A missing LLM
does not prevent deterministic Product V1 or CSV_V2 structured output.
