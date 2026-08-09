# P1V-M0 Original Valuation Method Recovery Matrix

Status: `VALUATION_GOLDEN_PARTIAL`

This is a bounded forensic result, not a valuation model. The search recovered the general direction “future EPS/earnings + assigned PE → target price → comparison with a reference price”, but it did not recover the original numerical workpapers needed to reproduce any of the three historical answers. No inferred value is Golden.

## Search scope and stop rule

The search was completed on 2026-08-10 across:

- the full repository, including hidden tracked files, docs, tests, fixtures and test data;
- the available local project workspace and all user-provided text attachments registered with Codex;
- the referenced ChatGPT conversation `6a6d7fb5-aae8-83ea-8f0a-70c1f086f804`, including all available older pages;
- the related available conversation `6a7575d0-92dc-83ea-8066-0b27e81eebb3` (“中际旭创跳水分析”);
- bounded local Codex session/artifact text matching the three company names and codes.

The required Chinese terms, codes, valuation terms and formula-like strings were searched. No internet search or reverse engineering from current market data was performed. The search now stops under the program’s bounded-search rule.

## Evidence inventory

| ID | Classification | Source artifact and location | What it proves | Golden numbers? |
|---|---|---|---|---|
| E1 | `DERIVED_SUMMARY` | P1V-M0 attachment, `pasted-text.txt:62-76` | The stated framework is future EPS/earnings, assigned PE, target price, then upside/downside. The same source says this framework is insufficient for implementation. | No |
| E2 | `SECONDARY_REFERENCE` | Phase 1 Master Program attachment, `pasted-text.txt:920-983` | The exact prior method for the three named companies was not in the supplied repository context. It explicitly prohibits substituting generic PE, PEG, DCF, consensus or industry PE. | No |
| E3 | `UNRELATED` | `app/services/company_research.py:252-262` | The current repository has an implied-growth scenario based on historical PE quantiles. This is a different method and cannot establish the original target-price method. | No |
| E4 | `UNRELATED` | ChatGPT thread `6a6d7fb5-aae8-83ea-8f0a-70c1f086f804`, turns `22d2e926-d549-46dc-83a7-d07d11d9c6c8` and `bfd301ab-2249-48b2-ba10-4b3e12969e37` | The recovered 980/981.33 calculation is an intraday VWAP/cost anchor and explicitly not a PE target price. | No |
| E5 | `SECONDARY_REFERENCE` | ChatGPT thread `6a7575d0-92dc-83ea-8066-0b27e81eebb3` | Current fundamental discussion mentions EPS and PE risks, but contains no original target-price workpaper for the three-stock method. | No |

No `PRIMARY_CALCULATION_EVIDENCE` was recovered. Therefore no historical number may be marked Golden.

## Recovery matrix

| Field | 300308 中际旭创 | 600406 国电南瑞 | 002112 三变科技 |
|---|---|---|---|
| Source artifact | E1/E2 framework; E4 is unrelated VWAP evidence | E1/E2 only | E1/E2 only |
| Source location | See evidence inventory | See evidence inventory | See evidence inventory |
| Calculation date | Missing | Missing | Missing |
| Reference stock price | Missing | Missing | Missing |
| Financial data as-of | Missing | Missing | Missing |
| Forecast year(s) | Missing | Missing | Missing |
| Revenue if used | Not established | Not established | Not established |
| Net profit if used | Not established | Not established | Not established |
| Total shares if used | Not established | Not established | Not established |
| Forecast EPS | Missing | Missing | Missing |
| EPS derivation | Missing | Missing | Missing |
| PE assumptions | Missing | Missing | Missing |
| Scenario assumptions | Missing | Missing | Missing |
| Target price | Missing | Missing | Missing |
| Upside/downside | Missing | Missing | Missing |
| Rounding | Missing | Missing | Missing |
| Explicitly recovered value | None | None | None |
| Inferred value | None; inference deliberately prohibited | None | None |
| Evidence confidence | `INSUFFICIENT_FOR_GOLDEN` | `INSUFFICIENT_FOR_GOLDEN` | `INSUFFICIENT_FOR_GOLDEN` |

## Proven method boundary

The only recovered direction is:

```text
estimate future EPS or earnings
→ assign PE multiples to forecast periods/scenarios
→ target_price = forecast_eps * target_pe
→ compare target price with a reference price
→ calculate upside/downside
```

Classification: `DERIVED_SUMMARY_ONLY`.

This chain is not promoted to an exact Golden formula because the original EPS source/derivation, PE assignment, scenario structure, forecast years, reference-price semantics, upside/downside convention and rounding are absent. In particular, the usual expression `(target_price / reference_price - 1) * 100` is not asserted as the historical formula.

## Completeness decision

The Golden is incomplete because every minimum deterministic field is missing: reference price/date, financial as-of date, forecast year, forecast EPS or its derivation inputs, target PE, historical outputs and rounding. The method remains:

```yaml
method_id: ORIGINAL_EPS_PE_TARGET_PRICE
version: UNRESOLVED
golden_complete: false
```

`docs/valuation/original_method_golden_spec.md`, immutable numerical fixtures and a reproduction calculator are intentionally not created. Creating them would manufacture evidence.

## Historical and current context contracts

- `HISTORICAL_REPRODUCTION`: must use immutable original as-of values and reproduce historical outputs. It is blocked until primary calculation evidence is supplied.
- `CURRENT_ANALYSIS`: must use current trusted data plus explicit current assumptions. It cannot overwrite historical evidence and remains blocked from numerical implementation in P1V-M1 while the method is unresolved.

## Data-source gap analysis

| Original input | Later acquisition class | Reason |
|---|---|---|
| Historical reference price/date | `HISTORICAL_VALUE_REQUIRED` | Must match the original calculation date, not today’s price. |
| Reported profit and shares outstanding | `AUTO_TRUSTED` | Can later come from versioned official filings if the original method used them. |
| Original manually estimated forecast profit/EPS | `MANUAL_REQUIRED` | A new consensus or provider value cannot replace a historical manual assumption. |
| Original PE assignment | `HISTORICAL_VALUE_REQUIRED` | It is a method assumption, not a value that should be inferred from industry PE. |
| Original scenario definitions | `HISTORICAL_VALUE_REQUIRED` | Scenario names, years and mappings must be recovered exactly. |
| Future current financial data | `AUTO_SINGLE_SOURCE` | Relevant only to a later current-analysis mode with explicit provenance. |

## Authority boundary

Valuation is `RESEARCH_EVIDENCE` and `executable=false`. It cannot create `ENTRY_ALLOWED`, override `NO_TRADE`, loosen a hard stop, raise a position cap, override a P1T veto, make CSV_V2 executable, auto-confirm/freeze or execute an order. High upside is never execution permission.

## Evidence needed to unblock P1V-M1

Supply an original screenshot, exported response, spreadsheet or note for each calculation showing the as-of date, reference price, forecast year/EPS (or profit and shares), PE per scenario, target price, upside/downside and rounding. Source line or cell locations must be preserved. Until then, P1V-M1 numerical implementation remains blocked.
