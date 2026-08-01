# Preview Side Effects 清单

## 1. 规则版本准备

- 入口：Application调用Repository的`ensure_rule_version()`。
- 实现：SQLAlchemy Repository委托`trade_plan.persistence.ensure_generator_rule_version`。
- 副作用：可能停用旧版本、创建新版本、激活并commit。
- 阶段H：集中为唯一Repository入口，行为和时机不变。

## 2. 默认规则集初始化

规则版本准备内部调用`ensure_default_rule_version`，空数据库下可能创建默认规则集/版本。该行为同样属于规则Repository副作用。

## 3. Confirm重新Preview

Persistence的`save_plan`调用Application重新生成Preview，因此会再次经过上述规则版本入口。它不是额外保存正式计划，但可能写规则版本。

## 4. AI重复Preview

AI分析仍通过兼容门面重新生成确定性Preview以绑定hash，也会经过Repository规则版本入口。

## 5. 无副作用部分

- Account/Holding/Profile/Bar/Quote读取适配器只读；
- PricePlanner纯计算；
- Feature、Strategy、Risk、Decision和Assembler不保存数据；
- 用户确认前仍不会创建正式TradePlan、TradePlanCheck或execution记录。

## 6. 后续计划

将规则版本初始化移到显式启动/迁移流程，并让Preview只读取已激活版本。此变化涉及部署和并发语义，不在阶段H处理。
