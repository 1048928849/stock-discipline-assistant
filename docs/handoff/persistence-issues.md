# Persistence Boundary 已知问题

## PI-001：Preview会创建并激活规则版本

- 当前行为：`generate_trade_plan`在读取规则参数前调用`ensure_generator_rule_version`。参数不完整时会停用旧版本、创建并激活新版本并立即commit。
- 为什么存在：历史实现通过preview入口自动升级生成器规则，保证旧数据库无需手工迁移也能获得完整参数。
- 风险：名义上的读取操作产生写入；并发preview可能竞争活动版本；外层事务被提前提交。
- 阶段G处理：逻辑已隔离到`trade_plan/persistence.py`，调用时机和行为不变。
- 后续计划：规则版本管理阶段引入显式启动迁移或幂等初始化，并定义并发与事务边界。

## PI-002：Confirm会重新执行完整Preview

- 当前行为：保存前重新执行Feature、Strategy、Risk和Decision并比较preview_hash。
- 为什么存在：防止用户确认时数据、规则或账户条件已经变化。
- 风险：重复计算且可能再次触发规则版本副作用。
- 阶段G处理：保留该安全检查，Persistence通过Application重新生成。
- 后续计划：引入带版本标识的不可变证据包，明确何时允许复用冻结Preview。

## PI-003：AI开启会重复执行确定性Preview

- 当前行为：AI分析路径重新调用旧兼容门面的preview函数。
- 为什么存在：AI证据需要绑定当前preview_hash。
- 风险：额外计算和规则版本写入机会。
- 阶段G处理：AI仍通过兼容门面调用新Application，输出不变。
- 后续计划：应用编排层显式传递同一TradePlanSnapshot给AI Evidence流程。

## PI-004：Application仍直接读取ORM

- 当前行为：Application接收Session并查询Account、Profile、Holding、Bar和Quote。
- 为什么存在：阶段G优先拆分正式保存与组装职责，避免同时改变数据访问和业务流程。
- 风险：Application测试仍需要数据库，Repository替换成本较高。
- 阶段G处理：Application禁止保存正式TradePlan，但保留读取以维持行为。
- 后续计划：建立只读TradePlanDataRepository，返回标准化应用输入。

## PI-005：Persistence包含execution初始化

- 当前行为：保存TradePlan和Checks后调用现有`initialize_plan_execution`，随后统一commit。
- 为什么存在：正式计划确认必须同时建立执行记录。
- 风险：计划持久化与执行上下文建立共享事务，失败恢复边界尚未显式建模。
- 阶段G处理：保留调用顺序和事务；Lifecycle只建立概念边界，不替换execution状态。
- 后续计划：以事务应用服务协调PlanRepository和ExecutionRepository。
