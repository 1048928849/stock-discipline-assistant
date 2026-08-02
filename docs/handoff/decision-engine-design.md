# Decision Engine 边界设计

## 1. 当前状态层次

当前系统存在三类容易混淆的状态：

1. `trade_plan_generator.generate_trade_plan_preview` 根据 12 个 Gate 生成兼容计划状态 `READY/WAIT/NO_TRADE/INSUFFICIENT_DATA`。
2. `one_click_pipeline._decision` 结合兼容计划状态和持仓触发器，生成用户可见的七类最终交易动作。
3. `workflow.create_trade_plan` 为手工录入计划生成 `BLOCKED/DRAFT/READY`；`assess_positions` 还生成执行阶段和 `allow_add`。这套旧工作流不输出七类动作，阶段 E 不统一它的状态词汇。

阶段 E 的 Decision Engine 接管前两项裁决。Feature、Strategy、金额风险计算和手工计划工作流保持不变。

## 2. 七类最终动作来源

### 2.1 BUY_PROHIBITED

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件：空仓，且 preview 兼容计划状态为 `NO_TRADE`。
- 上游 `NO_TRADE` 来源：`trade_plan_generator` 的 hard_fail；market、sector、large_cycle、platform、pullback、stop、reward_risk、position 任一 Gate 为“不通过”。

### 2.2 WAIT

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件：空仓，且不满足“READY 且允许当前买入”，同时不是 `NO_TRADE`。
- 包含上游 `WAIT`、`INSUFFICIENT_DATA`、READY 但试仓不足 100 股等情况。

### 2.3 TRIAL_ALLOWED

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件：空仓、preview 状态为 `READY` 且 `current_buy_allowed=True`。
- `current_buy_allowed` 当前等于 READY 且试仓数量至少 100 股。

### 2.4 HOLD

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件：持仓，且未触发止损/平台破位、减仓、确认加仓或 `NO_TRADE`。

### 2.5 CONDITIONAL_ADD

- 文件/函数：`app/services/trade_plan_generator.py` 与 `one_click_pipeline.py::_decision`
- 条件：持仓浮盈、兼容计划状态为 READY、当前价高于持仓止损价且未触发硬止损；并且优先级高于它的止损/减仓未触发。
- 当前生成器会在 READY 但不允许确认加仓时把兼容计划状态降为 WAIT。

### 2.6 REDUCE

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件一：持仓达到 `target_price`，形成 `first_reduction_triggered`。
- 条件二：持仓且 preview 状态为 `NO_TRADE`。
- 当前减仓优先于确认加仓。

### 2.7 PLAN_INVALID_EXIT

- 文件/函数：`app/services/one_click_pipeline.py::_decision`
- 条件：持仓触发硬止损，或平台事实 `platform_broken=True`。
- 这是持仓动作的最高优先级。

## 3. 状态、来源、优先级与目标

| 状态 | 当前来源 | 优先级 | 目标位置 |
|---|---|---:|---|
| PLAN_INVALID_EXIT | `_decision`：硬止损或平台破位 | 1 | Decision Engine |
| REDUCE | `_decision`：目标价触发或持仓 NO_TRADE | 2/4 | Decision Engine |
| CONDITIONAL_ADD | 生成器确认加仓条件 + `_decision` | 3 | Decision Engine |
| HOLD | `_decision`：持仓默认分支 | 5 | Decision Engine |
| TRIAL_ALLOWED | `_decision`：空仓 READY 且允许买入 | 空仓1 | Decision Engine |
| BUY_PROHIBITED | `_decision`：空仓 NO_TRADE | 空仓2 | Decision Engine |
| WAIT | `_decision`：空仓其余状态 | 空仓3 | Decision Engine |

持仓完整优先级保持：

```text
止损/平台破位 > 减仓 > 加仓 > NO_TRADE转减仓 > 持有
```

## 4. 上游兼容计划状态

生成器当前按以下顺序生成 `preview.status`：

1. hard_fail -> `NO_TRADE`
2. critical_unknown -> `INSUFFICIENT_DATA`
3. 突破、回踩或转强未通过 -> `WAIT`
4. 其他 -> `READY`
5. 持仓时，若 READY 但确认加仓不允许 -> `WAIT`

Decision Engine 将保留并返回这个兼容计划状态，供 API、保存逻辑和 Golden Master 使用；它不是新的 TradeDecision 枚举。

## 5. 目标输入边界

- StrategyEvaluationResult：只提供策略规则结果，不重新解释技术指标。
- RiskEvaluationResult：阶段 E 的只读兼容模型，承载已有 Gate 状态和“至少可交易 100 股”等已计算事实，不计算金额。
- PositionContext：是否持仓、价格/成本、止损与目标价、平台破位事实。
- MarketContext：当前市场/行业兼容上下文；市场风险上限仍由原流程计算。

DecisionContext 不包含 Session、ORM、Provider、Request 或 LLM。

## 6. 输出与兼容

DecisionResult 输出七类 TradeDecision、标签/原因、证据、阻断因素、下一步动作，并携带只为兼容旧 API 使用的 `legacy_plan_status`。应用适配层继续输出原字典字段：`status`、`label`、`next_action` 和 `rule_authority`。

## 7. workflow.py 的处理边界

`workflow.create_trade_plan` 的 `BLOCKED/DRAFT/READY` 是手工计划生命周期状态，不是七类最终交易动作；`assess_positions` 的 stage/logic_status/allow_add 属于旧执行检查。阶段 E 记录但不迁移，否则会在没有 StrategyResult 的路径中强行改变状态体系，违反“不统一状态、不改 API”的约束。

后续应在显式版本化兼容方案下，将手工计划工作流接入同一 Decision 输入契约。本阶段不会修改它。
