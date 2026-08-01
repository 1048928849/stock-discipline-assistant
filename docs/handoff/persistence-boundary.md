# Trade Plan Persistence Boundary

## 1. 当前数据库行为

| 行为 | 当前入口 | 目标位置 |
|---|---|---|
| 读取/创建/激活生成器规则版本 | `ensure_generator_rule_version` | `trade_plan/persistence.py` |
| 查询Account/Profile/Holding/Bar/Quote | preview生成函数 | Application的数据准备段；后续Repository化 |
| 保存TradePlan与冻结快照 | `save_generated_plan` | `trade_plan/persistence.py::save_plan` |
| 保存12项TradePlanCheck | `save_generated_plan` | `trade_plan/persistence.py::save_plan` |
| 关联AI分析 | `save_generated_plan` | `trade_plan/persistence.py::save_plan` |
| 初始化execution | `save_generated_plan` | Persistence调用现有execution服务 |
| 查询计划历史 | `plan_history` | `trade_plan/persistence.py::get_history` |
| 比较计划版本 | `compare_plans` | `trade_plan/persistence.py::compare_versions` |

## 2. Preview与正式保存边界

Preview路径不创建TradePlan，但当前会调用规则版本准备逻辑。正式保存必须：

1. 从SaveRequest重建PreviewRequest；
2. 重新生成preview；
3. 比较preview_hash，不一致返回PREVIEW_CHANGED；
4. 验证买入区与硬止损；
5. 计算plan_version和parent_plan_id；
6. 保存TradePlan、checks、AI关联和execution初始化；
7. 提交事务并返回原响应。

阶段G不改变这组时序和事务边界。

## 3. 快照边界

确认保存时继续冻结：

- engine_snapshot：完整preview；
- market_snapshot：市场与行业状态；
- account_snapshot：账户风险输入；
- source_snapshot：数据来源；
- preview_hash：规范化关键字段摘要。

TradePlanSnapshot领域对象表达冻结概念，但数据库仍使用现有JSON字段，不迁移表结构。

## 4. 规则版本副作用

`ensure_generator_rule_version` 会调用默认规则准备；当参数不完整时停用当前版本、创建1.1.0/1.2.0、激活并立即commit。阶段G将其移动到Persistence模块，但保持调用时机和提交行为。

该行为使preview不是完全只读。取消副作用需要独立迁移规则版本、并发和事务策略，不能在本阶段顺带修复。

## 5. Repository契约

`app/domain/trade_plan/contracts.py` 建立最小Persistence Protocol，描述保存、历史和比较能力。当前SQLAlchemy实现仍是函数式服务，避免在不改行为的阶段引入复杂Repository框架。

## 6. 禁止跨界

- Assembler、Compatibility和Lifecycle不得导入SQLAlchemy或ORM模型。
- Persistence不得重新计算Feature、Strategy或Risk。
- Application不得直接创建、flush或commit正式TradePlan。
- 旧API只通过兼容门面访问新模块，响应Schema不变。
