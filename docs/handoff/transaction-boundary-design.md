# Transaction Boundary设计

## 1. 当前事务现状

|流程|当前事务行为|问题|
|-|-|-|
|Preview生成|Application读取数据；规则初始化可能commit|纯计算和规则副作用未形成一个明确事务|
|Snapshot保存|Repository内部add、commit、refresh|保存失败无法与规则版本变更整体回滚|
|Confirm|读取Snapshot、映射计划、保存Gate、初始化执行，最后commit|职责集中；中途调用可能提前提交|
|RuleVersion|默认版本初始化和Manager均可能commit|调用方无法控制原子性|
|TradePlan保存|Persistence直接add、flush、commit|Mapper、Repository和事务职责混合|

## 2. 目标边界

应用入口拥有事务：

```text
Preview Application Transaction
  -> Preview Builder
  -> RuleVersionManager
  -> SnapshotRepository
  -> commit / rollback

Confirm Application Transaction
  -> load/verify Snapshot
  -> Legacy Adapter（仅缺失Snapshot）
  -> TradePlanMapper
  -> TradePlanRepository
  -> ExecutionInitializer
  -> commit / rollback
```

## 3. commit责任

- `transaction_scope`：唯一负责commit和rollback；
- RuleVersionManager：只add/flush并返回版本；
- SnapshotRepository：只查询、add/flush；
- TradePlanRepository：只查询、add/flush；
- TradePlanMapper：只构造内存中的ORM对象；
- ExecutionInitializer：只加入执行记录，不提交；
- Domain Service：不能感知事务。

## 4. 嵌套调用

普通Preview/Confirm入口没有活动事务时，`transaction_scope`创建并完成顶层事务。OneClick等已有活动事务调用时使用savepoint，不提前提交外层工作；异常回滚到savepoint并继续向上抛出，由外层决定最终回滚。

## 5. 失败语义

- Preview计算或Snapshot保存失败：规则版本和Snapshot一起回滚；
- Snapshot校验失败：不进入Mapper；
- TradePlan、Gate、AI关联或Execution任一步失败：正式计划整体回滚；
- Legacy重算hash变化：保持`PREVIEW_CHANGED`，不保存任何正式计划。
