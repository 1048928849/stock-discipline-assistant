# Product V1 Analysis Contract

This contract freezes the public boundaries used to deliver Product V1. Existing quality,
lineage, Evidence, package hashing, and freeze semantics remain authoritative.

## Data capabilities

| Capability | Subject | Semantic key | Stored value |
| --- | --- | --- | --- |
| `market.quote.realtime` | `stock/{symbol}` | `realtime/CNY` | `MarketQuote` |
| `market.quote.latest_close` | `stock/{symbol}` | `latest_close/CNY` | `MarketQuote` |
| `market.daily.qfq` | `stock/{symbol}` | `daily/qfq/CNY/share` | `MarketDailyBar` |
| `market.intraday.60m` | `stock/{symbol}` | `60m/qfq/CNY/share` | `MarketIntradayBar` |
| `market.turnover.daily` | `stock/{symbol}` | `daily/ratio` | `MarketTurnoverSnapshot` |
| `market.index_daily` | `index/{index}` | `daily/unadjusted/CNY/point` | `MarketIndexBar` |
| `market.breadth.daily` | `market/CN-A` | `daily/all-a` | `MarketBreadthSnapshot` |
| `market.amount.daily` | `market/CN-A` | `daily/CNY` | `MarketAmountSnapshot` |
| `market.industry.daily` | `industry/{industry}` | `daily/unadjusted/CNY/point` | `IndustryMarketSnapshot` |
| `market.industry.constituents` | `industry/{industry}` | `constituents/current` | `IndustryConstituentSnapshot` |
| `fundamental.profile` | `stock/{symbol}` | `profile` | `CompanyProfile` |
| `fundamental.financials` | `stock/{symbol}` | `financials` | existing financial tables |
| `announcement.catalog` | `stock/{symbol}` | `catalog/{start}/{end}` | `CompanyAnnouncement` |
| `company.concepts` | `stock/{symbol}` | `concepts/current` | `CompanyConcept` |
| `company.industry_chain` | `stock/{symbol}` | `industry-chain/current` | `CompanyChainPosition` |

Every formal value is returned as the value of the exact `ProviderResult` audited by
`DataHubRouter`. It binds capability, canonical `SubjectRef`, observed/fetched times, row count,
canonical digest, and `quality_record_id`. Persistence validates that exact result before an
atomic replacement and marks the quality record persisted only after the business flush.
`CONFLICTED`, `STALE`, and `MISSING` cannot replace trusted cache or become executable.

## Stable analysis interfaces

Analysis modules are pure functions over immutable Pydantic inputs and may not import providers,
SQLAlchemy, LLM clients, package builders, or persistence services.

- `analyze_intraday_turnover(TechnicalAnalysisInput) -> TechnicalContext`
- `analyze_market_regime(MarketRegimeInput) -> MarketRegime`
- `analyze_industry_mainlines(IndustryAnalysisInput) -> IndustryContext`
- `analyze_concept_chain(ConceptChainInput) -> ConceptChainContext`
- `build_trade_decision(TradeDecisionContext) -> TradeDecisionResult`

`ProductAnalysisSnapshot` is the single immutable input assembled after refresh/cache selection.
It contains data rows, effective-quality results, exact bindings, and one `analysis_started_at`.
No engine may re-read the database during an analysis.

## Decision contract

`TradeDecisionContext` combines the existing deterministic generator result with the four
analysis outputs and announcement risk. `TradeDecisionResult` owns `BuyPointAssessment`,
`PositionConstraints`, `RiskPlan`, and `ExitPlan`. The final position is the minimum of account,
symbol, market, industry, trend, entry, stop-distance, data-quality, and announcement caps.
Existing rule status, buy zone, hard stop, risk math, lot rounding, and the golden fixture remain
owned by `trade_plan_generator`; Product V1 constraints may only reduce or block new scenarios.

## Evidence and package

Evidence is created once by the package integration layer, never by analysis engines. Required
groups are price, daily structure, intraday structure, turnover, market regime, industry
mainline, concept/chain, company profile, financial, announcement risk, and decision rule.
Each required item contains capability, Subject, semantic key, exact quality record, observed and
fetched times, freshness/effective quality, digest, exact binding, and a bounded payload summary.

`DecisionPackage` retains all current fields and adds: `required_data_summary`, `market_regime`,
`industry_context`, `concept_chain_context`, `technical_context`, `buy_point_assessment`,
`position_constraints`, `risk_plan`, `exit_plan`, and structured `blocked_reasons`. Canonical JSON
hashing remains deterministic. AI may reference Evidence but cannot mutate any decision field.

At confirm, `freeze_trade_plan` revalidates every required exact binding, scope, observation/scan
range, digest, freshness, and current effective quality. Changed required Evidence returns a
reanalyze error. Optional supplemental news does not block solely because it changed.

## API and UI

The one-click response remains backward compatible (`steps`, `decision`, `plan`, `ai`,
`decision_package`, `can_save`) and adds `required_data`, `data_completeness`, `market_regime`,
`industry_context`, `concept_chain_context`, `technical_context`, and `trade_decision`.
The UI renders these API fields and never recomputes rules. It shows quality and cache/fallback
state, market/industry/company context, and the complete entry, sizing, stop, add, profit, reduce,
exit, no-trade, invalidation, and next-session plan.

## Dependency direction and ownership

`providers -> data_hub contracts/router/quality -> persistence/cache -> product snapshot ->
analysis -> decision -> package/freeze -> API -> UI`.

- `app/data_hub/*`, `app/providers/*`: capabilities, subjects, contracts, routing, freshness.
- `app/models.py`, Alembic, cache services: durable rows and exact lineage.
- `app/analysis/*`: pure deterministic analysis contracts and engines.
- `app/decision/*`: unified deterministic trade decision.
- `app/domain/models.py`, `package_builder.py`: Evidence and DecisionPackage only.
- `app/services/one_click_pipeline.py`, `plan_freeze.py`: one integration and one freeze path.
- `app/api/*`, `schemas_workflow.py`, templates/static assets: stable transport and presentation.

After each layer is implemented, the capability names, Subject/semantic keys, public analysis
models, decision models, Evidence groups, package fields, and API response keys above are frozen.
Changes require a demonstrated compatibility defect, not downstream convenience.
