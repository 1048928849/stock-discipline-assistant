# Risk Engine 边界设计

## 1. 阶段目标

Risk Engine 接管现有生成器中的确定性风险预算、买入价与止损价之间的风险、五类仓位数量上限、试仓数量和 A 股 100 股整手处理。它只消费已经准备好的价格和敞口，不判断策略是否通过，也不生成最终交易结论。

平台下沿、近 10 日低点与 ATR buffer 生成止损价格，以及平台高度和 R 倍数生成目标价的算法保持在上游价格计划段。本阶段不修改这些算法。Risk Engine 校验上游给出的 entry/stop/reward-risk，并完成账户风险与仓位控制。

## 2. 风险输入来源

| 输入 | 来源 | 用途 |
|---|---|---|
| Account.total_assets | 本地 Account，由应用层转换为纯值 | 账户权益、风险预算及仓位百分比基数 |
| Account.available_cash | 本地 Account，由应用层转换为纯值 | 可用现金允许数量 |
| risk_pct | TradePlanPreviewRequest | 账户权益乘风险比例 |
| max_position_pct | TradePlanPreviewRequest | 单股仓位上限 |
| max_total_position_pct | 请求；一键流程可能先按市场风险压缩 | 总仓位剩余额度 |
| max_industry_position_pct | TradePlanPreviewRequest | 行业集中度剩余额度 |
| Holding quantity/current_price | 当前股票已有持仓的纯值汇总 | 扣减单股剩余额度 |
| 全账户 Holding | 应用层查询后汇总为 total_position_value | 扣减总仓位剩余额度 |
| 同行业 Holding | 应用层查询后汇总为 sector_position_value | 扣减行业剩余额度 |
| Entry Price | 上游平台转强触发价 | 每股风险和各数量上限的价格分母 |
| Stop Price | 上游平台/低点/ATR算法 | 每股风险和止损距离 |
| reward_risk | 上游目标价算法 | 保持现有最低盈亏比 Gate判断 |
| Market Risk | one_click_pipeline | 高风险把总仓上限压至30%，中等压至60%；压缩后上限传入Risk |
| Sector Exposure | 公司行业与持仓汇总 | 行业集中度数量限制 |
| trial_position_ratio | 当前规则参数 | 最终允许数量转换为首次试仓数量 |

StrategyEvaluationResult 不参与风险公式。策略只决定规则匹配，Risk Engine 不包含 `if strategy_pass`。

## 3. 当前计算公式

### 3.1 风险预算

```text
risk_budget = account_equity × risk_pct ÷ 100
```

### 3.2 每股风险与止损距离

```text
per_share_risk = entry_price - stop_price
stop_distance_pct = per_share_risk ÷ entry_price × 100
```

entry 或 stop 缺失、或 entry 不高于 stop 时，风险数据不足，不计算仓位。

### 3.3 风险预算允许数量

```text
risk_allowed_quantity = floor_lot(risk_budget ÷ per_share_risk)
```

### 3.4 现金限制

```text
cash_allowed_quantity = floor_lot(available_cash ÷ entry_price)
```

### 3.5 单股限制

```text
single_remaining = max(0, equity × max_position_pct ÷ 100 - existing_symbol_value)
single_allowed_quantity = floor_lot(single_remaining ÷ entry_price)
```

### 3.6 总仓位限制

```text
total_remaining = max(0, equity × max_total_position_pct ÷ 100 - total_position_value)
total_allowed_quantity = floor_lot(total_remaining ÷ entry_price)
```

### 3.7 行业限制

```text
sector_remaining = max(0, equity × max_industry_position_pct ÷ 100 - sector_position_value)
sector_allowed_quantity = floor_lot(sector_remaining ÷ entry_price)
```

### 3.8 最终和试仓数量

```text
allowed_quantity = min(
  risk_allowed_quantity,
  cash_allowed_quantity,
  single_allowed_quantity,
  total_allowed_quantity,
  sector_allowed_quantity,
)

trial_quantity = floor_lot(allowed_quantity × trial_position_ratio)
```

`floor_lot` 保持原语义：先向下取整数股，再向下取100股整手，且不得小于0。

### 3.9 兼容展示值

```text
trial_amount = trial_quantity × entry_price
trial_account_pct = trial_amount ÷ equity × 100
maximum_loss = trial_quantity × per_share_risk
```

## 4. 状态与约束

- `INSUFFICIENT_DATA`：缺少有效 entry/stop，无法可靠计算。
- `BLOCKED`：计算完成但最终允许数量小于100股。
- `PASS`：最终允许数量至少100股。

constraints 逐项保存 risk_budget、cash、single_position、total_position、sector_exposure 的数量上限，并记录最先出现的最小值为 binding_constraint。该字段只增强领域可审计性，不增加 API 字段。

## 5. 系统边界

RiskContext 只包含 account_context、position_context、entry_context、stop_context、market_context 和 sector_context 六组只读纯值；不包含 Session、ORM、Provider、HTTP Request 或 LLM。

应用层仍负责查询 Account/Holding 并汇总持仓市值。Risk Engine 不读取数据库、不保存计划、不调用 Strategy或Decision Engine。
