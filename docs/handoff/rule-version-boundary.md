# Rule Version Boundary

## 1. RuleVersionManager

新增`app/services/rule_version_manager.py`，作为生成器规则版本的唯一写入口：

- `ensure_active_version(db)`：保持现有默认规则初始化、参数补齐、停用旧版本、创建/激活和commit行为；
- `create_snapshot_version(version)`：将ORM或Repository版本转换为不可变规则版本Snapshot，不写数据库。

`trade_plan.persistence.ensure_generator_rule_version`保留兼容委托，旧API和测试导入不变。

## 2. 调用边界

- Application只调用Repository的`ensure_rule_version()`；
- SQLAlchemy Repository调用RuleVersionManager；
- Persistence保存正式计划时调用RuleVersionManager；
- 其他Application模块禁止直接构造RuleVersion ORM。

## 3. 不重复创建

当活动版本已经包含全部GENERATOR_PARAMETERS时，Manager直接返回原版本，不停用、不新增、不commit。现有唯一约束`rule_set_id+version`保持。

## 4. Snapshot版本

PreviewSnapshot冻结：version字符串、parameters、rules及创建时有效日期/标识。Snapshot确认后即使活动规则版本变化，正式计划仍使用冻结preview内容；数据库TradePlan仍按现有方式关联确认时的活动RuleVersion ID，以保持Schema/API行为。

## 5. 已知副作用

`ensure_active_version`仍可能commit，这一行为未在阶段I取消。Snapshot保存也产生Preview写入，因此Preview尚非数据库意义上的纯读；本阶段解决的是正式业务计算冻结和Confirm免重算，而不是零写入。
