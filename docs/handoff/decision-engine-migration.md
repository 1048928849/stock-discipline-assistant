# Decision Engine 迁移记录

## 1. 已迁移状态

七类最终交易动作已迁移到 `app/services/decision_engine.py::evaluate_decision`：

- BUY_PROHIBITED
- WAIT
- TRIAL_ALLOWED
- HOLD
- CONDITIONAL_ADD
- REDUCE
- PLAN_INVALID_EXIT

生成器原 `READY/WAIT/NO_TRADE/INSUFFICIENT_DATA` 兼容计划状态的聚合也由 Decision Engine 返回为 `legacy_plan_status`，状态名称和 API 值没有改变。

## 2. 状态来源

- StrategyEvaluationResult 提供策略总体状态。
- RiskEvaluationResult 承载旧 Gate 状态、已计算的入场容量许可和阻断因素。
- PositionContext 提供持仓、当前价、成本、止损、目标价和平台破位事实。
- MarketContext 提供原 `next_observations`，不重复计算市场风险。

生成器使用真实 StrategyEvaluationResult 调用 Decision Engine。一键流程通过兼容适配器把已有 preview 重建为只读 Context，再调用同一个 `evaluate_decision`；适配器不包含状态优先级。

## 3. 保持的优先级

持仓动作严格保持：

```text
硬止损/平台破位 > 首次减仓 > 确认加仓 > NO_TRADE转减仓 > 持有
```

空仓动作严格保持：

```text
READY且可买入 > NO_TRADE > 其他等待
```

## 4. 未迁移逻辑

- Feature 和 Strategy 规则。
- 止损、目标价、风险收益与仓位金额计算。
- 市场风险对总仓位上限的压缩。
- `workflow.py` 手工计划的 BLOCKED/DRAFT/READY 生命周期状态。
- TradePlan、分析运行和执行审计持久化。
- API 序列化字段结构和数据库旧状态字符串。

## 5. 已知冲突

- `first_reduction_triggered` 与 `confirmation_add_allowed` 仍可同时为真；Decision Engine 保留减仓优先。
- 数据不足的兼容计划状态仍为 INSUFFICIENT_DATA，用户动作仍为 WAIT。
- 持仓且 `pattern=None` 的旧访问异常仍按原持仓分支时机保留。
- 持仓策略为 NO_TRADE 时仍映射为 REDUCE，而非新增退出状态。
- Gate 仍使用中文自由字符串。

以上问题均未在阶段 E 修复。

## 6. 无副作用边界

Decision领域模型与Decision Engine不导入SQLAlchemy、ORM模型、Provider、LLM或API对象，不读取数据库、不保存计划、不调用数据源。输入 Mapping 在领域模型建立时复制并只读包装。
