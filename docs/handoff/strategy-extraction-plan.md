# Strategy Engine 抽取计划

## 1. 目标与约束

阶段 D 只把“平台突破—回踩确认”的策略事实解释和触发判断从
`app/services/trade_plan_generator.py` 移到纯 Strategy Engine。Feature Engine 继续负责事实计算；旧生成器继续负责数据库读取、风险与仓位、旧状态裁决、持仓处理和 API 序列化。

本阶段不改变算法、阈值、条件顺序、旧状态、API、数据库、前端或 Golden Master。Strategy 不接收 Session、Provider、LLM、HTTP Request 或 ORM 对象。

## 2. 当前职责分类

### A. Strategy Logic（本阶段迁移）

- 解释 `multi_timeframe`：周线向下为失败，月周大周期向上为通过，其他情况为等待。
- 解释 `platform_structure.valid_platform`：判断平台结构是否成立。
- 解释首次突破与突破成交量：无突破为未知/等待，有突破但量能不足为等待，突破且量能确认才通过。
- 解释回踩结构：平台破位为失败；已回踩且缩量为通过；已回踩但未缩量为等待；未出现回踩为未知/等待。
- 解释再次转强：`turned_stronger` 为真才通过，否则未知/等待。
- 汇总上述策略规则为内部 `PASS/FAIL/WAIT/UNKNOWN`，但不生成旧计划状态。

平台识别、突破、回踩和转强的数值计算已经属于 Feature Engine，本阶段不改其算法。市场、行业和数据新鲜度虽会影响最终交易许可，但仍由显式上下文和旧闸门处理；不凭空新增策略条件。

### B. Risk Logic（保留）

- 平台下沿、近 10 日低点和 ATR buffer 形成的止损价。
- 止损距离上限、平台高度目标、最低 R 倍数、第一/第二目标价。
- 账户风险预算、每股风险、现金上限、单股/行业/总仓位上限。
- 已有仓位金额扣减、最终数量取最小值和 100 股整手向下取整。
- 试仓比例、试仓金额、账户占比和最大损失。
- 高/中市场风险对总仓位上限的压缩仍在一键分析流程，不迁移。

### C. Decision Logic（保留）

- 12 个旧 Gate 的中文状态以及 `critical_unknown`、`hard_fail` 聚合。
- `READY/WAIT/NO_TRADE/INSUFFICIENT_DATA` 的生成。
- 持仓浮盈、硬止损、首次减仓、确认加仓及其优先级。
- 一键流程对用户七类结论的映射，包括 `BUY_PROHIBITED`、`TRIAL_ALLOWED`、`HOLD`、`CONDITIONAL_ADD`、`REDUCE`、`PLAN_INVALID_EXIT`。
- 数据不足对外仍映射为等待；状态统一延后到 Decision Engine 阶段。

### D. Persistence Logic（保留）

- `ensure_generator_rule_version` 读取、停用、创建、提交和激活规则版本。
- Account、CompanyProfile、Holding、MarketDailyBar、MarketQuote 的查询。
- `save_generated_plan` 保存 TradePlan、TradePlanCheck、AI 分析关联和执行初始化。
- preview hash、engine/market/account/source snapshot 和计划历史/比较。
- 一键分析运行、缓存、默认账户和审计写入。

## 3. 现有 12 项 Gate 映射

| 原文件 | 原函数/代码段 | 业务含义 | 目标规则 ID | 本阶段是否迁移 |
|---|---|---|---|---|
| `trade_plan_generator.py` | market gate | 市场环境 | `market_context` | 保留：旧裁决上下文 |
| `trade_plan_generator.py` | sector gate | 行业/板块强弱 | `sector_context` | 保留：旧裁决上下文 |
| `trade_plan_generator.py` | mode gate | 交易模式和周期 | `trade_mode` | 保留：展示/上下文 |
| `trade_plan_generator.py` | large_cycle gate | 月线、周线大周期趋势 | `large_cycle_direction` | 是：策略事实解释 |
| `trade_plan_generator.py` | platform gate | 日线平台结构成立 | `platform_structure` | 是：策略事实解释 |
| `trade_plan_generator.py` | breakout gate | 突破与量能确认 | `breakout_volume_confirmation` | 是：策略触发判断 |
| `trade_plan_generator.py` | pullback gate | 回踩、缩量、平台破位 | `pullback_structure` | 是：策略有效性判断 |
| `trade_plan_generator.py` | turn_stronger gate | 再次放量转强 | `turn_stronger_confirmation` | 是：策略触发判断 |
| `trade_plan_generator.py` | stop gate | 硬止损明确且距离合格 | `stop_validity` | 保留：Risk Logic |
| `trade_plan_generator.py` | reward_risk gate | 目标价与盈亏比合格 | `reward_risk` | 保留：Risk Logic |
| `trade_plan_generator.py` | position gate | 账户与集中度允许仓位 | `position_capacity` | 保留：Risk Logic |
| `trade_plan_generator.py` | data gate | 数据完整性和新鲜度 | `data_quality` | 保留：旧裁决/数据边界 |

此外，Strategy Engine 增加一个不改变旧条件的输入完整性规则 `platform_data_sufficiency`：只把 Feature 缺失或质量为 `MISSING` 映射为内部 `UNKNOWN`，兼容层仍生成原来的五个“无法判断”Gate。

## 4. 迁移适配边界

`generate_trade_plan_preview` 仍准备 FeatureSnapshot，并把纯映射上下文构造成 StrategyContext。注册表返回 `PlatformBreakoutPullbackStrategy`，其结果由兼容适配器转换为原五个 Gate 字典；旧生成器随后原样计算止损、收益风险、仓位和最终状态。

兼容层必须保持原 Gate 的 code、name、中文 status、evidence、source、data_time 和 missing_conditions。StrategyResult 本身不得携带旧计划状态或资金数量。

## 5. 隐藏副作用与已知行为

- 生成 preview 时，`ensure_generator_rule_version` 可能停用旧规则版本、创建并激活新版本且提交事务。
- AI 开启时，`trade_plan_ai` 会再次调用 `generate_trade_plan_preview`，重复执行确定性 preview。
- 一键流程 `_default_account` 可能创建默认账户。
- 行情、市场和行业流程可能写缓存或回退到缓存。
- 一键分析与 AI 分析会写运行记录、缓存命中信息和审计记录。
- 持仓最终结论优先级位于一键 Decision 映射：止损/破位、减仓、加仓、NO_TRADE、持有。
- 减仓触发与确认加仓允许可以同时为真，当前依靠外层优先级选择减仓。
- `position_mode=持仓` 且 `pattern=None` 存在已知异常，本阶段只记录，不修复。

以上副作用和已知行为全部留在 Strategy Engine 之外。

## 6. 实施状态

| 项目 | 状态 |
|---|---|
| 领域枚举、不可变模型和 Strategy Protocol | 已迁移 |
| StrategyRegistry | 已迁移 |
| 大周期方向规则 | 已迁移 |
| 平台结构规则 | 已迁移 |
| 突破量能规则 | 已迁移 |
| 回踩结构规则 | 已迁移 |
| 再次转强规则 | 已迁移 |
| 旧 Gate 兼容适配 | 已迁移 |
| Risk Logic | 保留 |
| Decision Logic | 延后 |
| Persistence Logic | 保留 |
| 已知行为问题 | 发现问题；已记录于 `strategy-extraction-issues.md` |
