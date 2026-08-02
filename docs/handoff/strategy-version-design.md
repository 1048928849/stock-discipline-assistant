# Strategy Version System设计

## 1. 目标与边界

Strategy Management负责策略资产的定义、版本、生命周期、冻结快照和交易引用；现有Strategy Engine继续负责规则执行。管理层不计算Feature、不裁决交易、不计算风险和价格，也不引入第二策略或回测。

## 2. 领域结构

```text
StrategyDefinition
  id / name / description / category / owner / status / created_at
  |
  +-- StrategyVersion
        version / status
        rule_snapshot / parameter_snapshot
        created_at / activated_at / retired_at
        |
        +-- StrategyLifecycleEvent
```

策略ID是稳定业务标识；版本记录使用数据库ID作为TradePlan引用，并以`strategy_id + version`保证唯一。

## 3. 生命周期

```text
DRAFT -> RESEARCH -> SHADOW -> ACTIVE
                             ACTIVE -> SUSPENDED -> ACTIVE
                             ACTIVE -> RETIRED
                          SUSPENDED -> RETIRED
```

- DRAFT：初始定义，可编辑；
- RESEARCH：研究整理中；
- SHADOW：影子验证，不参与正式策略替换；
- ACTIVE：正式可用，规则与参数快照不可修改；
- SUSPENDED：暂停新关联，保留历史引用；
- RETIRED：永久退休，不允许重新激活。

每次转换写入生命周期事件。非法跳转由领域生命周期服务拒绝。

## 4. 版本规则与冻结

- 版本采用三段式语义版本，如`1.0.0`、`1.1.0`；
- ACTIVE版本的`rule_snapshot`和`parameter_snapshot`不可修改；
- 修改已激活策略必须从旧版本复制并创建新版本；
- 新版本不会覆盖或删除旧版本；
- 旧TradePlan始终保存原`strategy_version_id`，后续激活新版本不迁移历史引用。

## 5. 与Strategy Engine关系

当前唯一受管理策略：

```text
strategy_id: platform_breakout_pullback
version: 1.0.0
status: ACTIVE
engine: PlatformBreakoutPullbackStrategy
```

数据库版本与代码中的`strategy_id / strategy_version`一致。运行时继续执行现有`PlatformBreakoutPullbackStrategy.evaluate()`；管理Service仅确保对应ACTIVE资产存在并提供持久化引用，不改变任何规则结果。

## 6. 与TradePlan关系

TradePlan新增：

- `strategy_id`：稳定策略业务ID；
- `strategy_version_id`：冻结版本记录ID。

迁移会创建平台突破策略1.0.0并回填已有TradePlan。新Confirm在同一事务内确保管理版本存在并写入引用。字段保持可空以兼容手工计划和旧部署数据，服务层负责正式生成计划的关联完整性。

## 7. 数据库与兼容

新增表：`strategies`、`strategy_versions`、`strategy_lifecycle_events`。迁移只增加表和引用列，不修改既有交易字段、API响应或Preview Hash。SQLite使用普通新增列避免重建`trade_plans`；MySQL使用相同标准SQLAlchemy类型。

## 8. Repository与Service

- Repository：查询、add、flush，不提交事务；
- Lifecycle：纯转换校验；
- Service：创建策略、创建/复制版本、查询历史、激活、暂停、退休；
- Application事务边界负责commit/rollback。
