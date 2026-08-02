# TradePlan Persistence Final Design

## 1. 结构

```text
app/services/persistence/
  preview_snapshot_repository.py
  trade_plan_mapper.py
  trade_plan_repository.py
  execution_initializer.py
```

## 2. 职责

### PreviewSnapshotRepository

按`account_id + symbol + preview_hash`幂等保存和读取冻结Snapshot，只执行SQLAlchemy查询、add和flush，不commit。

### TradePlanMapper

把已验证Preview、SaveRequest、RuleVersion和版本信息转换为TradePlan ORM对象；不查询数据库、不保存、不初始化执行。

### TradePlanRepository

负责计划版本查询、add/flush、Gate保存、AI分析关联、历史查询和版本比较。Repository不判断Strategy、Risk、Decision或价格。

### ExecutionInitializer

封装现有`initialize_plan_execution`，只创建执行初始记录；事务由Confirm Application控制。

## 3. 兼容门面

`app/services/trade_plan/persistence.py`保留既有函数名和导入路径，委托新组件，避免API、测试和OneClick调用变化。

## 4. 禁止依赖

- Mapper不得依赖Session；
- Repository不得调用Feature/Strategy/Risk/Decision/PricePlanner；
- 所有Persistence组件不得commit或rollback；
- ExecutionInitializer不得改变执行状态优先级。
