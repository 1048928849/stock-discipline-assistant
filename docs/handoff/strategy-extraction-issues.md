# Strategy Engine 抽取阶段已知问题

阶段 D 只记录以下既有行为，不修复、不调整优先级，也不更新 Golden Master。

## SEI-001：持仓且平台事实为空时异常

- 文件：`app/services/trade_plan_generator.py`
- 函数：`generate_trade_plan_preview`
- 当前行为：`position_mode=持仓` 且 `pattern=None` 时，后续持仓/计划路径可能访问缺失的平台结构并异常。
- 风险：行情缺失时持仓检查可能无法返回降级结果。
- 是否影响 Golden Master：当前数据不足案例为空仓，未覆盖该组合。
- 计划处理阶段：Strategy Engine 稳定后单独修复，并先增加回归测试。

## SEI-002：减仓与确认加仓可同时成立

- 文件：`app/services/trade_plan_generator.py`、`app/services/one_click_pipeline.py`
- 函数：`generate_trade_plan_preview`、`_decision`
- 当前行为：达到目标价且浮盈、规则为 READY 时，`first_reduction_triggered` 与 `confirmation_add_allowed` 可同时为真；外层优先选择减仓。
- 风险：内部计划表达冲突，调用者若绕开外层裁决可能得到歧义。
- 是否影响 Golden Master：是；`case_05c_reduce` 固定了同时为真的行为和减仓优先级。
- 计划处理阶段：Decision Engine 状态和优先级显式化阶段。

## SEI-003：数据不足对外映射为 WAIT

- 文件：`app/services/trade_plan_generator.py`、`app/services/one_click_pipeline.py`
- 函数：`generate_trade_plan_preview`、`_decision`
- 当前行为：内部计划为 `INSUFFICIENT_DATA`，用户七类结论映射为 `WAIT`。
- 风险：内部状态和用户结论语义不一致，审计时需同时查看两个字段。
- 是否影响 Golden Master：是；`case_01_insufficient_data` 固定该映射。
- 计划处理阶段：Decision Engine 状态统一阶段。

## SEI-004：Gate 使用中文自由字符串

- 文件：`app/services/trade_plan_generator.py`
- 函数：`_gate`、`generate_trade_plan_preview`
- 当前行为：Gate 状态使用 `通过/不通过/警告/无法判断`，没有数据库级或 API 级枚举约束。
- 风险：拼写和跨层映射可能漂移。
- 是否影响 Golden Master：是；所有 Golden Master 均固定这些字符串。
- 计划处理阶段：后续兼容迁移阶段；先建立版本化映射再统一。

## SEI-005：预览会创建并激活规则版本

- 文件：`app/services/trade_plan_generator.py`
- 函数：`ensure_generator_rule_version`、`generate_trade_plan_preview`
- 当前行为：读取型 preview 可能停用旧版本、创建新版本、提交事务并激活它。
- 风险：预览不是无副作用操作，并发执行可能产生规则版本竞争。
- 是否影响 Golden Master：测试环境通过基线规则准备间接依赖该行为。
- 计划处理阶段：Persistence/Application Service 边界重构阶段。

## SEI-006：AI 开启时重复执行确定性 preview

- 文件：`app/services/trade_plan_ai.py`、`app/services/one_click_pipeline.py`
- 函数：AI 分析入口、`run_one_click_analysis`
- 当前行为：一键流程已生成一次确定性计划，AI 分析路径会再次调用 `generate_trade_plan_preview`。
- 风险：重复计算和数据库副作用，两个 preview 间的数据变化还可能造成证据版本差异。
- 是否影响 Golden Master：AI 关闭案例固定了“关闭不影响规则计划”；AI 开启的重复行为由既有测试覆盖。
- 计划处理阶段：应用编排和 AI Evidence 边界阶段。
