# 当前状态体系清单

## 1. 结论

当前系统没有单一状态模型，而是并存“计划生成状态、用户交易结论、旧计划状态、规则闸门状态、执行状态、一键运行状态和步骤状态”。多数状态以数据库自由字符串和普通字典传递，Schema 没有统一枚举约束。本阶段只记录，不修改。

## 2. 状态总表

| 类型 | 当前值 | 来源 | 是否保留 |
| --- | --- | --- | --- |
| 计划生成结论 | `READY` | `trade_plan_generator.generate_trade_plan_preview`；前端 `planStatusLabel` | 待决定；需映射到 V2 `TRIAL_ALLOWED` 或持仓结论 |
| 计划生成结论 | `WAIT` | `trade_plan_generator`；前端 `planStatusLabel` | 待决定；语义可保留，类型需统一 |
| 计划生成结论 | `NO_TRADE` | `trade_plan_generator`；前端 `planStatusLabel` | 待决定；建议兼容映射到 `BUY_PROHIBITED` |
| 计划生成结论 | `INSUFFICIENT_DATA` | `trade_plan_generator`；前端 `planStatusLabel` | 待决定；当前一键层映射为 `WAIT`，语义有损 |
| 旧计划状态 | `DRAFT` | `TradePlan.status` 默认值；`workflow.py` | 兼容期保留，最终与生命周期状态分离 |
| 旧计划状态 | `BLOCKED` | `workflow.create_trade_plan` 和前端计划卡 | 待决定；与 `NO_TRADE`/`BUY_PROHIBITED` 重叠 |
| 用户交易结论 | `BUY_PROHIBITED` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `WAIT` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `TRIAL_ALLOWED` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `HOLD` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `CONDITIONAL_ADD` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `REDUCE` | `one_click_pipeline._decision` | 目标七结论之一，建议保留 |
| 用户交易结论 | `PLAN_INVALID_EXIT` | `one_click_pipeline._decision` | 待统一为目标值 `EXIT`；兼容层暂时保留 |
| Gate/规则状态 | `通过` | `trade_plan_generator._gate` 及各 Gate 判断 | 待统一为 `PASS` |
| Gate/规则状态 | `不通过` | 同上 | 待统一为 `FAIL` |
| Gate/规则状态 | `警告` | 同上；表示条件存在但非硬失败 | 待统一，通常映射为 `WAIT`，需逐规则确认 |
| Gate/规则状态 | `无法判断` | 同上；数据或条件不足 | 待统一为 `UNKNOWN` |
| 目标规则状态 | `PASS`、`FAIL`、`WAIT`、`UNKNOWN` | V2 任务书；当前尚未实现 | 下一阶段领域模型目标 |
| 执行状态 | `draft` | `TradePlan.execution_status` 默认值 | 待决定；与计划状态分离后可保留 |
| 执行状态 | `confirmed` | `initialize_plan_execution` | 建议保留 |
| 执行状态 | `waiting_entry` | `initialize_plan_execution` | 建议保留 |
| 执行状态 | `entry_triggered` | 初始化或人工评估 | 建议保留 |
| 执行状态 | `partially_executed` | 手工成交 | 建议保留 |
| 执行状态 | `holding` | 持仓初始化或成交 | 建议保留 |
| 执行状态 | `add_triggered` | 人工评估 | 待统一 |
| 执行状态 | `reduce_triggered` | 人工评估 | 待统一 |
| 执行状态 | `stop_triggered` | 人工评估 | 建议保留 |
| 执行状态 | `take_profit_triggered` | 人工评估 | 待统一 |
| 执行状态 | `invalidated` | 人工评估 | 建议保留 |
| 执行状态 | `closed` | 卖出后净持仓为零 | 建议保留 |
| 一键运行状态 | `running`、`success`、`failed`、`confirmed` | `PlanAnalysisRun.status` / `one_click_pipeline` | 保留为工作流运行状态，不应混入交易结论 |
| Pipeline 步骤状态 | `success`、`partial`、`failed`、`skipped` | `one_click_pipeline._step` | 保留为步骤状态，不应混入规则状态 |
| AI 分析状态 | `pending`、`running`、`success`、`failed`、`not_configured`、`skipped` | `trade_plan_ai`、一键流程与前端 | 保留为 AI 任务状态，不影响规则结论 |

## 3. 来源分布

### Backend

- `app/services/trade_plan_generator.py`：四个计划生成状态和四种中文 Gate 状态。
- `app/services/one_click_pipeline.py`：七个当前用户结论及其优先级。
- `app/services/workflow.py`：旧的 `DRAFT / READY / BLOCKED` 状态。
- `app/services/plan_execution.py`：12 个执行状态及事件迁移。
- `app/models.py`：`TradePlan.status`、`TradePlanCheck.status`、`execution_status` 均为字符串列。

### Schema

- `schemas_workflow.py` 对交易模式、市场/行业状态、持仓输入等使用 `Literal`。
- 返回的计划状态、Decision、Gate 和执行状态没有统一响应领域枚举。
- AI Schema 中 `RuleConclusion.status` 仍是任意字符串，但内容由后端冻结并校验相等。

### Frontend

- `app/static/app.js:planStatusLabel` 映射四个计划生成状态。
- 一键结果直接使用后端 `decision.status` 生成 CSS class，没有集中枚举表。
- `executionStatusLabels` 映射全部 12 个执行状态。
- 旧计划卡仍单独识别 `READY` 和 `BLOCKED`。

## 4. 当前优先级行为

持仓 Decision 的实际优先级为：

```text
硬止损或平台破坏
  > 第一减仓触发
  > 确认加仓允许
  > preview=NO_TRADE 时建议减仓
  > HOLD
```

空仓 Decision 的实际优先级为：

```text
preview=READY 且 current_buy_allowed -> TRIAL_ALLOWED
preview=NO_TRADE                    -> BUY_PROHIBITED
其余（含 INSUFFICIENT_DATA）       -> WAIT
```

因此 `INSUFFICIENT_DATA` 在对外 Decision 层会丢失为 `WAIT`；`PLAN_INVALID_EXIT` 与目标 `EXIT` 名称不一致；持仓时内部 `preview.status=READY` 也可能因为不满足浮盈加仓而被改成 `WAIT`，但对外仍返回 `HOLD`。

## 5. 统一时的安全约束

- 必须保留计划评估、用户决策、执行生命周期、工作流运行、AI 任务五个不同状态域。
- 状态迁移应通过兼容映射落地，不能直接替换数据库历史字符串。
- 先用 Golden Master 固定现状，再引入领域 Enum/值对象。
- 前端展示标签应消费后端稳定枚举或共享映射，避免继续复制业务判断。
