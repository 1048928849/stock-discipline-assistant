# Preview Snapshot 设计

## 1. 当前Preview生成路径

```text
API / OneClick / AI
  -> trade_plan_generator兼容门面
  -> trade_plan.application.generate_trade_plan
  -> Repository确保规则版本并读取数据
  -> Feature -> Strategy -> PricePlanner -> Risk -> Decision -> Assembler
  -> compatibility.legacy_preview
  -> preview_digest
```

当前Preview不会创建正式TradePlan，但Repository的规则版本入口可能写数据库并commit。

## 2. 当前Confirm路径

`trade_plan.persistence.save_plan`从SaveRequest重建PreviewRequest，直接重新执行完整Application，比较新preview_hash与请求hash，一致后创建TradePlan、TradePlanCheck、execution并commit。

阶段I目标为：先按account_id、symbol、preview_hash读取服务端PreviewSnapshot，验证完整性后直接转正式计划；不存在Snapshot时保留原重算流程，并在内部记录`legacy_recalculate_confirm=true`。

## 3. Hash位置与语义

旧`preview_hash`由`trade_plan.compatibility.preview_digest`对关键兼容字段计算，用于API和Golden Master，不能改变。

Snapshot新增独立完整性hash，对冻结的完整snapshot内容计算。数据库同时保存：

- preview_hash：旧API查找键；
- snapshot_hash：完整Snapshot完整性校验；
- payload：完整旧Preview JSON。

Confirm先校验请求preview_hash，再验证snapshot_hash，任一失败拒绝。

## 4. Rule Version位置

当前创建逻辑是`trade_plan.persistence.ensure_generator_rule_version`，内部调用`workflow.ensure_default_rule_version`，可能停用旧版本、创建1.1.0/1.2.0、激活并commit。Repository适配器调用该函数。

阶段I迁移为RuleVersionManager唯一入口：`ensure_active_version()`负责兼容创建，`create_snapshot_version()`生成冻结规则版本切片。Application不创建版本。

## 5. 数据库写入位置

- Preview：当前只有规则版本副作用；阶段I新增preview_snapshots写入。
- Confirm：Persistence创建TradePlan、Checks，关联AI，初始化execution并commit。
- OneClick：另有PlanAnalysisRun审计写入。
- AI：TradePlanAIAnalysis缓存与审计写入。

## 6. AI调用位置

`trade_plan_ai.run_ai_analysis`重新通过兼容门面生成Preview并核对hash。阶段I该调用也会保存/复用PreviewSnapshot，但AI仍不参与Snapshot计算和确认裁决。

## 7. 冻结内容

PreviewSnapshot包含：

- snapshot_id、symbol、created_at、hash；
- feature_snapshot：pattern、multi_timeframe、chart和数据状态；
- strategy_snapshot：策略规则、Gate及状态原因；
- risk_snapshot：position_calculation、账户风险输入及风险Gate；
- decision_snapshot：兼容计划状态、允许买入和持仓触发证据；
- price_snapshot：buy_plan、exit_plan、confirmation_add；
- rule_version_snapshot：版本、规则名和参数；
- account_snapshot；
- market_snapshot：市场/行业上下文、sources和data_date；
- preview_payload：用于无损恢复旧API/正式计划保存。

领域对象递归冻结，Snapshot Service不计算策略、风险、价格或AI内容。

## 8. 兼容策略

- API请求和响应字段不变，不要求客户端提交snapshot_id。
- Snapshot按account_id+symbol+preview_hash检索。
- 旧客户端、旧Preview或已清理Snapshot走legacy重算。
- Golden Master只比较响应，新增数据库行不改变hash。
- Snapshot表为新增表，不影响旧数据和现有外键。
