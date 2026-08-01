# Price Planning Domain 设计

## 1. 职责

PricePlanner只根据Feature中的平台事实、标准化行情Frame和现有价格参数生成PricePlan。它不读取账户、数据库，不判断策略是否通过，不计算仓位，也不保存TradePlan。

## 2. 输入

- StrategyEvaluationResult：作为显式边界输入，不新增条件；现有算法仍以Feature的`valid_platform`事实为准。
- FeatureSnapshot中的`platform_structure`。
- market_data：用于原近10日最低价计算。
- atr_buffer_multiple。
- minimum_reward_risk。
- buy_zone_atr_multiple，固定兼容值0.2。

## 3. 输出PricePlan

- entry_zone：原转强价±0.2 ATR。
- entry_reference：转强触发价。
- stop_price：`max(平台下沿, 近10日最低价) - ATR×buffer`。
- first_target：平台高度目标与最低R目标取较大值。
- second_target：约3R目标。
- target_price：兼容指向first_target。
- reward_risk：first_target对应盈亏比。
- invalidation_price：平台下沿。
- structure_invalidation：原中文结构失效说明。

PricePlan使用冻结dataclass，缺失条件用None表达，不生成READY/WAIT等状态。

## 4. 原公式与顺序

```text
如果pattern存在且valid_platform：
  atr_buffer = atr14 × atr_buffer_multiple
  recent_low = 最近10根Low最小值
  stop = round(max(platform_lower, recent_low) - atr_buffer, 4)
  entry = round(turn_trigger_price, 4)

如果stop < entry：
  platform_target = upper + (upper - lower)
  minimum_r_target = entry + minimum_reward_risk × (entry - stop)
  first_target = round(max(platform_target, minimum_r_target), 4)
  second_target = round(entry + 3 × (entry - stop), 4)
  reward_risk = (未round的first_target - entry) / (entry - stop)

buy_low/high = round(entry ± atr14 × 0.2, 4)
```

顺序、round位置和float转换必须保持，避免Golden Master数值漂移。

## 5. 边界情况

- pattern缺失或平台无效：所有交易价格为None。
- stop不低于entry：保留entry和stop，但目标及reward_risk为None。
- market_data缺失：与pattern缺失一起返回空PricePlan。
- 不凭空推导目标或止损。
