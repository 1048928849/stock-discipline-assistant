# Confirm Flow V2

## 1. 新流程

```text
SaveRequest(account_id, symbol, preview_hash)
  -> get_preview_snapshot
  -> 校验preview_hash查找键
  -> verify_hash完整性
  -> 使用snapshot.preview_payload
  -> 冻结TradePlanSnapshot
  -> 保存TradePlan/Checks/execution
```

Snapshot命中时不执行Feature、Strategy、PricePlanner、Risk或Decision。

## 2. Legacy兼容流程

Snapshot不存在时：

```text
legacy_recalculate_confirm=true
  -> 重建PreviewRequest
  -> 完整Application重算
  -> 比较preview_hash
  -> 保存Snapshot
  -> 保存正式计划
```

该标记记录在内部确认元数据/返回对象的服务端流程中，不增加旧API字段。旧数据和未经过新兼容门面的调用仍可确认。

## 3. Hash失败

- 请求hash找不到Snapshot：允许legacy重算；若重算hash不同，保持PREVIEW_CHANGED 409。
- Snapshot存在但完整性hash不匹配：拒绝确认，不降级重算，避免掩盖存储损坏。
- Snapshot payload中的preview_hash与请求不一致：拒绝确认。

## 4. Snapshot生命周期

- Preview完成后立即保存Snapshot；相同account/symbol/preview_hash幂等复用，不重复创建。
- Snapshot本身不可修改；数据库适配器只创建或读取。
- Confirm不删除Snapshot，便于审计及同一hash的兼容版本确认。
- 正式TradePlan仍按原版本规则生成parent_plan_id和plan_version。

## 5. 不变项

- 请求/响应字段、状态、Gate和execution不变；
- Strategy、Risk、Decision、PricePlanner不变；
- AI无权修改Snapshot结论；
- 用户确认前不创建正式TradePlan。
