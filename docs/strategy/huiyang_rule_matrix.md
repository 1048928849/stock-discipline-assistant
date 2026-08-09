# Huiyang Research Rule Matrix

## Source and page-note

Both research DOCX files were read completely from OOXML, including body paragraphs, tables,
headers, footers, footnotes and endnotes. The core manual contains 230 body records and the full
process textbook contains 159. Neither file contains footnotes or endnotes.

The files save only two logical page markers. Therefore `source_page` below means the **saved
logical page**, not a claimed physical Word page. No physical page number was invented. The
source files are local research inputs and are excluded from Git under `.codex-input/`.

Source abbreviations:

- `CORE`: 汇阳财策老师交易内核：周期演变规律与执行手册
- `FLOW`: 汇阳财策交易体系全流程教材：消息面、看盘、选股、交易纪律

## Normalized rules

| rule_key | source_document | source_section | source_page | normalized_rule | rule_category | required_data | required_frequency | deterministic | parameterizable | existing_product_v1_equivalent | compatibility | implementation_decision | rejection_reason | reason_code | test_case |
|---|---|---|---:|---|---|---|---|---|---|---|---|---|---|---|---|
| HY-DATA-001 | CORE/FLOW | 总纲/五层漏斗 | logical 2 | Data quality and auditable lineage pass before planning. | DATA_GATE | stock/index/industry quality | per analysis | yes | yes | PV1-DATA-001 | EXISTING | HARD_GATE | - | DATA_AUDIT_REQUIRED | missing lineage blocks |
| HY-CYCLE-001 | CORE | 第二章周期演变 | logical 2 | Classify the cycle before interpreting a stock signal. | MARKET_CYCLE | breadth, industry trend, continuity | daily | yes | yes | PV1-MKT-001 | COMPATIBLE_EXTENSION | CSV_V2 state model | - | CYCLE_CONTEXT_EVALUATED | all finite states |
| HY-THEME-001 | FLOW | 五层漏斗/主攻方向 | logical 2 | A message is not a main theme until sector participation confirms it. | MAIN_THEME | industry breadth and continuity | daily/intraday | yes | yes | PV1-IND-001 | SAME_MEANING | context gate | - | MAIN_THEME_REQUIRES_CONFIRMATION | message without sector |
| HY-IND-001 | CORE | 资金从试探到聚焦 | logical 2 | Industry continuity requires multi-session relative strength, not one spike. | INDUSTRY_CONTINUITY | industry and CSI300 history | daily | yes | yes | PV1-IND-001 | COMPATIBLE_EXTENSION | context score | - | INDUSTRY_CONTINUITY_EVALUATED | one-day spike rejected |
| HY-ROLE-001 | CORE | 第四章周期载体 | logical 2 | Leader/capacity roles require rank, weight, liquidity and persistent lead evidence. | STOCK_ROLE | constituents, rank, amount | daily | yes | yes | none | COMPATIBLE_EXTENSION | structured role classifier | - | STOCK_ROLE_EVIDENCE_REQUIRED | missing rank gives UNKNOWN |
| HY-RS-001 | CORE | 点线面时/板块配合 | logical 2 | Compare stock with industry and CSI300 over 20/60 sessions. | RELATIVE_STRENGTH | aligned closes | daily | yes | yes | technical comparison | COMPATIBLE_EXTENSION | deterministic indicators | - | RELATIVE_STRENGTH_ALIGNED | misaligned dates |
| HY-TREND-001 | CORE | 五日线与趋势资金 | logical 2 | Trend uses moving-average order, slope and high/low structure. | TREND_STRUCTURE | qfq daily bars | daily | yes | yes | PV1-TECH-001 | COMPATIBLE_EXTENSION | technical evidence group | - | TREND_STRUCTURE_EVALUATED | bullish/bearish arrays |
| HY-MOM-001 | CORE | 第三章验证工具 | logical 2 | RSI/MACD are grouped momentum evidence and cannot independently trigger entry. | MOMENTUM | qfq closes | daily | yes | yes | PV1-TECH-001 | COMPATIBLE_EXTENSION | grouped score only | - | MOMENTUM_NOT_SOLE_TRIGGER | high RSI only |
| HY-VP-001 | CORE | 承接与持续性 | logical 2 | Breakout requires volume; pullback quality requires contracting volume. | VOLUME_PRICE | OHLCV | daily | yes | yes | PV1-TECH-001 | COMPATIBLE_EXTENSION | deterministic triggers | - | VOLUME_PRICE_CONFIRMATION | breakout low volume |
| HY-SR-001 | CORE/FLOW | 支撑位是承接区 | logical 2 | Support and resistance are ATR-buffered zones, never a single magic number. | SUPPORT_RESISTANCE | OHLC, SMA, ATR, swings | daily | yes | yes | PV1-ENTRY-001 | COMPATIBLE_EXTENSION | deterministic price plan | - | SUPPORT_ZONE_CALCULATED | zone order |
| HY-INTRA-001 | CORE | 开盘价：当天成本锚 | logical 2 | Opening-price acceptance requires intraday data. | INTRADAY_ACCEPTANCE | opening and minute bars | intraday | yes | yes | none | REQUIRES_INTRADAY_DATA | pending only | daily bars cannot prove it | PENDING_INTRADAY_CONFIRMATION | daily-only request |
| HY-INTRA-002 | CORE | 盘中均线：日内平均成本 | logical 2 | Recovery of intraday average within a bounded interval needs minute bars. | INTRADAY_ACCEPTANCE | minute bars | intraday | yes | yes | none | REQUIRES_INTRADAY_DATA | pending only | daily bars cannot prove it | PENDING_INTRADAY_CONFIRMATION | no minute bars |
| HY-ENTRY-001 | FLOW | 买点按周期阶段分 | logical 2 | Entry needs all hard gates, a daily trigger, intraday confirmation and valid risk/reward. | ENTRY_TRIGGER | context, OHLCV, intraday, risk | per decision | yes | yes | PV1-ENTRY-002 | COMPATIBLE_EXTENSION | ENTRY_ALLOWED state | - | ENTRY_CONFIRMATIONS_REQUIRED | score alone rejected |
| HY-POS-001 | CORE/FLOW | 第一笔最多轻仓 | logical 1/2 | First position is a configurable percentage bounded by risk and cash. | POSITION_RULE | account, cash, stop | per decision | yes | yes | PV1-RISK-002 | SAME_MEANING | position plan | - | INITIAL_POSITION_BOUNDED | missing account gives percent only |
| HY-ADD-001 | CORE/FLOW | 亏损不加仓 | logical 1/2 | A losing position cannot be increased. | ADD_RULE | cost, current price, position | per decision | yes | no | risk engine | SAME_MEANING | HARD_GATE | - | LOSS_POSITION_ADD_BLOCKED | current below cost |
| HY-ADD-002 | CORE | 回关/滚动 | logical 2 | Add only when profitable, context and role hold, trigger reconfirms and budget remains. | ADD_RULE | holding, context, role, risk | per decision | yes | yes | risk engine | COMPATIBLE_EXTENSION | holding plan | - | PROFITABLE_ADD_REQUIRES_RECONFIRMATION | one condition missing |
| HY-REDUCE-001 | FLOW | 三段取关 | logical 2 | Reduce on failed acceptance, support break or context deterioration; do not average down. | REDUCE_RULE | price, support, context | intraday/daily | yes | yes | PV1-EXIT-001 | COMPATIBLE_EXTENSION | reduce conditions | - | STRUCTURE_REDUCE_TRIGGER | support break |
| HY-EXIT-001 | CORE | 周期结束是地位丧失 | logical 2 | Exit when invalidation, trend destruction or persistent loss of role is proven. | EXIT_RULE | stop, trend, role, industry | per scan | yes | yes | PV1-EXIT-001 | COMPATIBLE_EXTENSION | EXIT state | - | STRUCTURAL_EXIT_TRIGGERED | hard stop/decline |
| HY-LOSS-001 | CORE/FLOW | 日亏限制 | logical 1/2 | Stop adding/trading at configurable daily loss percentage. | DAILY_LOSS_RULE | realized/unrealized daily PnL | realtime | yes | yes | portfolio risk | COMPATIBLE_EXTENSION | parameterized hard gate | fixed 3000/6000 rejected | DAILY_LOSS_LIMIT_REACHED | threshold reached |
| HY-T1-001 | FLOW | T+1风险纪律 | logical 2 | New-position size must allow overnight gap risk under T+1. | T1_RULE | position, volatility, risk budget | per entry | yes | yes | risk engine | COMPATIBLE_EXTENSION | risk override | - | T1_NEW_POSITION_RISK_EXCEEDED | oversized new entry |
| HY-CAT-001 | CORE | 消息强度四问 | logical 2 | Catalyst may create a hypothesis but cannot directly increase executability. | CATALYST_RULE | sourced catalyst and market feedback | event/daily | yes | yes | evidence only | COMPATIBLE_EXTENSION | explanation/context only | - | CATALYST_REQUIRES_MARKET_VALIDATION | catalyst only |
| HY-CAT-002 | FLOW | 消息到题材 | logical 2 | Rumor never raises entry permission or score ceiling. | CATALYST_RULE | catalyst classification | event | yes | no | AI boundary | SAME_MEANING | explicit no-op | - | RUMOR_CANNOT_RAISE_EXECUTABILITY | rumor input |
| HY-HIGH-001 | CORE | 赶顶与加速末端 | logical 2 | A climax state blocks a new entry and switches an existing position to defense. | MARKET_CYCLE | RSI, ATR, acceleration, holding | daily | yes | yes | risk engine | COMPATIBLE_EXTENSION | hard gate/mode switch | - | CLIMAX_NEW_ENTRY_BLOCKED | high RSI/ATR |
| HY-NDET-001 | CORE/FLOW | 老师结论词转事实 | logical 2 | “Looks strong” has no effect without explicit observable evidence. | NON_DETERMINISTIC | none | any | no | no | none | NON_DETERMINISTIC | excluded from engine | subjective phrase | NON_DETERMINISTIC_EXCLUDED | phrase scan |
| HY-NDET-002 | CORE | 主控/资金维护 | logical 2 | “Has control” cannot be inferred from prose or one bar. | NON_DETERMINISTIC | order book and repeatable definition absent | intraday | no | possible later | none | NON_DETERMINISTIC | excluded from engine | undefined evidence | NON_DETERMINISTIC_EXCLUDED | no order-book contract |
| HY-REJ-001 | CORE/FLOW | 30万/1格/3000/6000 | logical 1/2 | Fixed currency examples must not become universal production constants. | REJECTED | account and risk settings | config | yes | yes | risk configuration | CONFLICT | reject literal constants | example-specific | FIXED_ACCOUNT_EXAMPLE_REJECTED | varying account sizes |
| HY-REJ-002 | CORE/FLOW | 支撑位 | logical 2 | Reaching a displayed support price alone is not a buy trigger. | REJECTED | price only | realtime | yes | no | entry engine | CONFLICT | reject direct trigger | omits acceptance | PRICE_TOUCH_NOT_ENTRY | touch without confirmation |
| HY-REJ-003 | CORE/FLOW | 消息不是买点 | logical 2 | News title alone cannot trigger a formal trade. | REJECTED | headline only | event | yes | no | AI/evidence boundary | SAME_MEANING | reject direct trigger | lacks feedback | MESSAGE_DIRECT_BUY_REJECTED | headline only |
| HY-REJ-004 | CORE | 龙头/容量中军 | logical 2 | Do not assign LEADER/CAPACITY_CORE without constituent evidence. | REJECTED | no rank/weight | daily | yes | no | none | SAME_MEANING | return UNKNOWN | missing facts | ROLE_CLAIM_WITHOUT_RANK_REJECTED | missing constituents |
| HY-MKT-OPT | CORE | 市场周期 | logical 2 | Missing breadth means UNKNOWN/BREADTH_UNAVAILABLE, never neutral or safe. | DATA_GATE | MarketRegimeSnapshot | daily | yes | no | PV1-MKT-001 | REQUIRES_FULL_MARKET_DATA | lower ceiling, advisory continues | optional for selected stock | BREADTH_UNAVAILABLE | no breadth snapshot |
| HY-IND-OPT | CORE | 板块大于个股 | logical 2 | Missing industry permits technical-only research, not a full actionable plan. | DATA_GATE | profile/membership and sector history | daily | yes | no | PV1-IND-001 | REQUIRES_FULL_MARKET_DATA | hard gate and UNKNOWN role | optional degraded mode | INDUSTRY_CONTEXT_UNAVAILABLE | BSE missing industry |
| HY-RR-001 | FLOW | 明确无效点 | logical 2 | No clear invalidation or insufficient reward/risk blocks entry regardless of score. | ENTRY_TRIGGER | zone, stop, target | per decision | yes | yes | PV1-RISK-001 | SAME_MEANING | HARD_GATE | - | RISK_REWARD_INSUFFICIENT | reward/risk below default |

## Matrix counts and execution boundary

- Normalized rules: 32
- Existing/same-meaning rules: 10
- Compatible extensions: 14
- Conflicts: 2
- Intraday-only rules: 2
- Full-market/industry dependent rules: 2
- Non-deterministic rules: 2
- Rejected executable interpretations: 4

Only rows with `deterministic=yes` and an explicit implementation decision can enter CSV_V2.
`NON_DETERMINISTIC`, `REJECTED`, and `REQUIRES_INTRADAY_DATA` rows never obtain a synthetic daily
confirmation. Source research language remains evidence input, not executable program text.
