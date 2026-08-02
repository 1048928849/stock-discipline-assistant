# Strategy Research Platform设计

## 1. Research与Strategy的区别

Strategy定义可执行规则、参数快照和生命周期；Research记录这些规则背后的可验证假设、逻辑依据、适用边界、失效条件、证据和验证结论。

```text
StrategyVersion（执行资产）
  |
  +-- StrategyResearchRecord（研究核心）
  +-- EvidenceRecord[]（证据追加日志）
  +-- ValidationRecord[]（验证追加日志）
```

Research只用于研究治理和审计，不被Feature、Strategy Engine、Risk、Decision、PricePlanner或交易流程读取。

## 2. 研究核心

每个StrategyVersion最多一个StrategyResearchRecord，包含：

- hypothesis：可验证假设；
- thesis：策略主张和依据；
- causal_chain：从环境到触发、风险控制和退出的因果链；
- market_conditions：适用市场环境；
- applicable_scenarios：适用场景；
- failure_conditions：逻辑失效条件；
- created_at：首次冻结时间。

ACTIVE或曾经激活的StrategyVersion不可修改研究核心；必须创建新版本。证据和验证记录是追加式资产，可继续补充，但不重写历史记录。

## 3. Evidence模型

证据类型：

```text
MANUAL
MARKET_DATA
FINANCIAL_REPORT
NEWS
CASE_STUDY
```

EvidenceRecord保存`type / source / content / reference / created_at`并绑定StrategyVersion。证据内容不自动改变规则、版本状态或交易结论。

## 4. Validation模型

验证状态：

```text
PENDING
RUNNING
PASSED
FAILED
```

ValidationRecord保存验证类型、状态、样本量、结果摘要和创建时间。验证记录采用追加方式；新结果不会覆盖旧结论，以保留研究演进轨迹。

## 5. 与StrategyVersion关系

StrategyVersion领域对象增加可选`research_record`以及`evidence_records / validation_records`。Strategy Research Service按版本ID创建和查询，Strategy Management Repository在读取版本时聚合关联研究资产。

## 6. 当前策略迁移

`platform_breakout_pullback@1.0.0`迁移时写入基础研究核心，覆盖：

- 平台突破、缩量回踩、再次转强的当前假设；
- 规则来源为现有纪律策略实现；
- 适用于日线趋势波段和非明显下降环境；
- 数据不足、平台失效、放量跌回和趋势破坏等已知限制。

同时写入一条MANUAL来源证据，标识该研究记录来自现有已验证方法的结构化迁移。

## 7. 与未来回测系统关系

未来回测或影子验证只能作为Validation生产者：向版本追加样本量、状态和结果摘要。Research Platform不包含回测执行器，不依赖Backtrader，也不允许验证结果直接覆盖交易规则或激活版本。

## 8. Persistence与事务

- Repository只查询、add、flush，不commit；
- Service使用既有`transaction_scope`；
- 新表通过外键绑定StrategyVersion，删除版本时级联清理研究资产；
- API交易字段、Preview Hash和TradePlan流程保持不变。
