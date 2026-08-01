# Confirm Flow Final

## 1. Snapshot确认

```text
SaveRequest
  -> transaction_scope
  -> SnapshotRepository.load
  -> verify_hash
  -> TradePlanMapper
  -> TradePlanRepository.save + Gates + AI link
  -> ExecutionInitializer
  -> commit
```

确认模式记录为`SNAPSHOT_CONFIRM`。Snapshot命中时不重新调用Feature、Strategy、Risk、Decision和PricePlanner。

## 2. Legacy确认隔离

只有Snapshot不存在时进入LegacyConfirmAdapter：

```text
LEGACY_RECALCULATE
  -> 重建PreviewRequest
  -> 原Application重算
  -> 校验preview_hash
  -> 保存Snapshot
  -> 进入统一Mapper/Persistence流程
```

Legacy路径保留且不改变API；内部确认元数据新增`confirm_mode`，同时保留`legacy_recalculate_confirm`布尔标记。

## 3. 原子性

以下内容属于同一Confirm事务：

- Legacy路径产生的规则版本与Snapshot；
- TradePlan与版本关系；
- TradePlanCheck；
- AI分析关联；
- execution初始事件和摘要。

任一步失败均rollback，不留下部分正式计划。

## 4. 不变项

- API字段和HTTP状态不变；
- 正式计划版本规则不变；
- Snapshot hash和旧preview_hash语义不变；
- 执行初始化逻辑不变；
- Strategy、Feature、Risk、Decision、PricePlanner均不修改。
