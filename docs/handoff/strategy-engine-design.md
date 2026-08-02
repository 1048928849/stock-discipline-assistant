# Strategy Engine 设计

## 1. 职责

Strategy Engine 只解释标准化 Feature 和显式 Context，输出不可变的内部规则结果。它回答“当前事实是否符合平台突破—回踩确认策略”，不计算资金、仓位、止损、目标价，也不生成用户最终交易结论。

当前调用链：

```text
FeaturePipeline -> FeatureSnapshot -> StrategyRegistry
-> PlatformBreakoutPullbackStrategy.evaluate
-> StrategyEvaluationResult -> 旧 Gate 兼容适配
-> 旧 Risk/Decision/Persistence/API 流程
```

## 2. Feature 与 Strategy 边界

- Feature Engine 计算平台上下沿、平台有效性、突破对象、量比、回踩事实、破位事实、转强事实和多周期方向。
- Strategy Engine 不读取 K 线，不调用技术指标函数；它只解释 `platform_structure` 和 `multi_timeframe`。
- Strategy Engine 不回写 FeatureSnapshot。测试固定了评估前后快照完全一致。

## 3. PlatformBreakoutPullbackStrategy

- 策略 ID：`platform_breakout_pullback`
- 内部版本：`1.0.0`，仅用于领域结果标识；未新增数据库策略版本体系。
- 注册方式：`default_strategy_registry()` 显式注册唯一策略。
- 评估顺序：数据充分性、大周期、平台、突破量能、回踩结构、再次转强。
- 汇总：关键 Feature 缺失为 `UNKNOWN`；任一失败为 `FAIL`；全部通过为 `PASS`；其他触发未齐为 `WAIT`。

## 4. 实际规则清单

| 规则 ID | 类别 | 原有含义 |
|---|---|---|
| `platform_data_sufficiency` | REQUIRED | Feature 是否具备策略解释所需结构；不新增交易要求 |
| `large_cycle_direction` | CONTEXT | 周线向下失败；月周大周期向上通过；其他等待 |
| `platform_structure` | REQUIRED | 现有 `valid_platform` 是否成立 |
| `breakout_volume_confirmation` | TRIGGER | 突破是否发生及原量能条件是否确认 |
| `pullback_structure` | HARD_REJECT | 回踩是否出现、是否缩量、是否破位 |
| `turn_stronger_confirmation` | TRIGGER | 原 `turned_stronger` 事实是否确认 |

## 5. StrategyContext 字段来源

| 字段 | 来源 |
|---|---|
| `symbol` | TradePlanPreviewRequest |
| `feature_snapshot` | FeaturePipeline |
| `parameters` | `GENERATOR_PARAMETERS` 与当前 RuleVersion parameters 合并结果 |
| `position_mode` | TradePlanPreviewRequest |
| `position_context` | 生成器准备的纯值上下文；当前只传 `has_position` |
| `market_context` | 请求市场状态与行情缺失原因 |
| `sector_context` | 请求行业状态 |

所有 Mapping 在 StrategyContext 建立时复制并用只读代理包装。Context 不含 Session、ORM、Provider、Request 或 LLM 客户端。

## 6. Result 与旧流程映射

`StrategyEvaluationResult` 包含 strategy_id、strategy_version、overall_status、rules、reasons、next_observations、missing_data。每条规则包含统一状态、类别、原因、证据和缺失数据。

`app/services/strategy_evaluation.py` 将五条迁移规则映射回原 Gate：

- PASS -> 通过
- FAIL -> 不通过
- WAIT -> 警告
- UNKNOWN -> 无法判断

兼容器复用规则证据中的 source_id/data_time，并保留原 Gate code、中文名称、证据文本和 missing_conditions。Strategy Result 不出现 READY、NO_TRADE、七类结论或仓位金额。

## 7. trade_plan_generator 剩余职责

- 数据库查询及规则版本自动创建/激活。
- FeaturePipeline 调用和 StrategyContext 的应用层准备。
- 市场、行业、模式、止损、盈亏比、仓位、数据质量 Gate。
- 止损、目标价、买入区、风险预算和仓位数量。
- 旧状态裁决、持仓浮盈/止损/减仓/加仓兼容逻辑。
- preview 序列化、hash、保存、历史和比较。

## 8. 无副作用约束

Strategy 与 rules 模块没有 SQLAlchemy、Provider、LLM、API 或持久化依赖；规则函数只读取 Context/Feature 并返回冻结 dataclass。注册表不从数据库或动态代码加载策略。数据库副作用仍留在旧应用服务中，并在已知问题文档记录。

## 9. 未来新增策略的标准步骤

1. 明确策略只需要哪些标准化 Feature，缺失事实先扩展 Feature Engine。
2. 在 `app/strategies/<strategy_id>/` 实现纯规则与 Strategy Protocol。
3. 使用唯一 strategy_id 显式注册。
4. 增加规则状态、缺失数据、不可变性和无副作用单元测试。
5. 在应用层显式选择策略；不得在 Strategy Registry 内自动评分或组合。
6. 为旧 API 新增版本化兼容映射前，先建立 Golden Master。

## 10. 下一阶段 Decision Engine 输入边界

Decision Engine 未来应读取 StrategyEvaluationResult、Risk 计算结果、数据质量、市场/行业上下文和持仓执行上下文，然后统一裁决旧计划状态与用户七类结论。它不应重新解释平台 Feature，也不应让 Strategy 直接生成 READY 或用户交易结论。阶段 D 不实现该层。
