# Risk Engine 迁移记录

## 1. 已迁移函数与逻辑

- `_floor_lot` 实现迁移为 `app.services.risk_engine.floor_lot`；生成器保留兼容别名供旧调用方使用。
- 账户权益乘风险比例形成风险预算。
- 买入价减止损价形成每股风险。
- 买入价与止损价形成止损距离百分比及原阈值状态。
- 风险预算、现金、单股、总仓位、行业集中度五类数量上限。
- 五类数量取最小值、100股整手向下取整。
- 最终允许数量按原试仓比例形成试仓数量。
- 试仓金额、账户占比、最大损失及原公式说明。
- 原 position Gate 的通过、不通过、无法判断映射。

## 2. 未迁移逻辑

- 平台下沿、近10日低点和ATR buffer生成止损价格。
- 平台高度、最低R目标和第二目标价格。
- Feature、Strategy和Decision规则。
- Account/Holding数据库查询与持仓市值汇总。
- 市场高/中风险对总仓上限的30%/60%压缩。
- TradePlan保存、快照和API序列化。

## 3. 风险参数

Risk Engine原样消费以下参数，不提供新默认值：

- risk_pct
- max_position_pct
- max_total_position_pct
- max_industry_position_pct
- trial_position_ratio
- maximum_stop_distance_pct
- minimum_reward_risk

所有值仍来自当前请求和活动规则版本。没有调整阈值或参数优先级。

## 4. 输入输出

RiskContext输入六类只读纯值：账户、持仓、入场、止损、市场和行业上下文。应用层负责把ORM数据转换成金额汇总。

RiskEvaluationResult输出：

- RiskStatus：PASS/BLOCKED/INSUFFICIENT_DATA
- allowed_quantity
- risk_amount
- risk_ratio
- constraints与binding_constraint
- blocking_reasons
- calculation_details

`legacy_position_calculation`只负责将领域结果投影为原`position_calculation`字典，不增加或删除API字段。

## 5. 与Strategy和Decision的边界

- Risk Engine不接收StrategyResult，也不判断策略是否通过。
- Decision Engine继续消费旧Gate状态和“试仓数量至少100股”的已计算事实。
- Risk Engine不生成READY、WAIT、NO_TRADE或七类最终交易动作。
- Decision Engine代码在阶段F未修改。

## 6. 兼容说明

Golden Master固定以下行为：持仓市值扣减、五类数量最小值、整手取整、试仓比例、Gate顺序及中文证据。迁移后这些值保持不变，Snapshot不更新。

Risk领域和服务无SQLAlchemy、Provider、LLM或API依赖，不读取或写入数据库。
