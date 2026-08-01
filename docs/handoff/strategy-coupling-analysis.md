# 平台突破—回踩确认策略耦合分析

## 1. 当前策略定位

生产计划策略的正式名称为：

```text
日线趋势波段：平台放量突破—缩量回踩—再次转强
```

前端称为“平台突破—回踩确认”。当前它不是可插拔策略，而是交易计划生成 service 的固定实现。回测模块中的四个演示策略与它没有统一接口。

## 2. 能力位置清单

| 能力 | 文件 | 函数/符号 |
| --- | --- | --- |
| 策略名称、硬性禁止项、加仓原则 | `app/services/trade_plan_generator.py` | `GENERATOR_RULES` |
| 默认参数 | `app/services/trade_plan_generator.py` | `GENERATOR_PARAMETERS` |
| 参数版本写入/激活 | `app/services/trade_plan_generator.py` | `ensure_generator_rule_version` |
| K 线标准读取 | `app/services/technical_snapshots.py` | `load_qfq_frame` |
| MA/MACD/RSI/ATR 等指标 | `app/services/technical.py` | `prepare_indicators` |
| 月线、周线、日线方向 | `app/services/trade_plan_generator.py` | `_direction` |
| 平台识别 | `app/services/trade_plan_generator.py` | `_platform_pattern`：滚动 20 日上沿/下沿与区间振幅 |
| 突破判断 | `app/services/trade_plan_generator.py` | `_platform_pattern`：收盘突破比例、候选突破 |
| 放量判断 | `app/services/trade_plan_generator.py` | `_platform_pattern`：突破量/平台均量与 `breakout_volume_multiple` |
| 回踩识别 | `app/services/trade_plan_generator.py` | `_platform_pattern`：容差、回踩区、缩量比 |
| 再次转强 | `app/services/trade_plan_generator.py` | `_platform_pattern`：触发价、前高、VOL_MA20 |
| 市场状态 | `app/services/one_click_pipeline.py` | `_market_assessment` |
| 行业相对强弱 | `app/services/one_click_pipeline.py` | `_sector_assessment` |
| 市场风险下总仓位上限 | `app/services/one_click_pipeline.py` | `run_one_click_analysis` 中 `effective_total_cap` |
| 12 道规则闸门 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview`、`_gate` |
| 买入区间 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview`：转强价 ± 0.2 ATR |
| 硬止损 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview`：平台下沿/近 10 日低点与 ATR buffer |
| 目标价和盈亏比 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview` |
| 仓位计算 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview`、`_floor_lot` |
| 内部计划结论 | `app/services/trade_plan_generator.py` | `generate_trade_plan_preview`：`READY/WAIT/NO_TRADE/INSUFFICIENT_DATA` |
| 用户交易结论 | `app/services/one_click_pipeline.py` | `_decision` |
| AI 证据边界 | `app/services/trade_plan_ai.py` | `build_evidence_package`、`validate_ai_output` |
| 计划保存与快照 | `app/services/trade_plan_generator.py` | `save_generated_plan`、`_preview_digest` |
| 一键入口编排 | `app/services/one_click_pipeline.py` | `run_one_click_analysis` |
| API 入口 | `app/api/workflow.py` | `analyze_and_generate_plan`、preview/save/confirm 路由 |
| 请求/AI Schema | `app/schemas_workflow.py` | `TradePlanPreviewRequest`、`OneClickPlanRequest`、AI Schema |
| ORM 持久化 | `app/models.py` | `RuleSet`、`RuleVersion`、`TradePlan`、`TradePlanCheck` |
| 状态和规则展示 | `app/static/app.js` | `planStatusLabel`、`showTradePlan`、`renderOneClick`、`loadTradePlans` |
| 页面结构 | `app/templates/index.html` | 交易计划表单和结果区域 |
| 既有测试 | `tests/test_trade_plan_generator.py`、`tests/test_one_click_pipeline.py` | 形态、仓位、AI、保存和一键流程测试 |

## 3. 主要耦合

### 3.1 定义与版本耦合

`ensure_generator_rule_version` 在评估路径中读取并可能停用旧版本、创建新版本和提交事务。策略定义不是纯配置；一次 preview 可能产生数据库写入。

### 3.2 特征与规则耦合

`_platform_pattern` 同时完成指标准备、平台/突破/回踩/转强特征计算和布尔规则判断。输出是无类型字典，规则层按字符串键读取。

### 3.3 规则与持久化耦合

`generate_trade_plan_preview` 直接接收 SQLAlchemy `Session`，查询账户、持仓、行情、公司资料和规则版本，计算所有规则后再序列化完整 API 字典。

### 3.4 规则与编排耦合

一键流程在调用生成器前计算市场、行业和市场风险仓位上限；调用后又把内部状态转换为用户 Decision。完整结论不是由单一 Decision Resolver 负责。

### 3.5 规则与前端耦合

前端硬编码策略名称、状态标签、12 道闸门展示、仓位解释、加仓/减仓/退出文案。若后端状态直接重命名，前端显示会产生行为变化。

## 4. 隐藏业务逻辑

- 规则版本可能在“读取规则/生成预览”期间自动升级并提交。
- 市场高风险把总仓位上限压到 30%，中等风险压到 60%，该逻辑位于一键编排层而非生成器。
- 持仓只有浮盈、内部状态 READY、价格高于已有止损时才允许确认加仓；否则生成器会把内部 READY 改为 WAIT。
- Decision 对持仓按“止损/破位 > 减仓 > 加仓 > NO_TRADE > 持有”排序。
- 减仓触发时 `confirmation_add.allowed` 仍可能为 true，但 Decision 优先返回 REDUCE。
- 空仓数据不足时内部为 `INSUFFICIENT_DATA`，对外 Decision 是 `WAIT`。
- 持仓模式且 `preview.pattern is None` 时，`_decision` 对 `None.get(...)` 会抛 `AttributeError`；本阶段记录但不修复。
- 一键“分析”不是完全只读：可能创建默认账户、更新行情/研究缓存、创建规则版本并保存 `PlanAnalysisRun`。
- 保存时重新计算 preview 并校验 `preview_hash`，不是直接信任浏览器提交的计划字段。
- AI 开启时会再次生成确定性 preview；AI 结果不能修改冻结字段。

## 5. 下一阶段抽取边界建议

下一阶段应先建立兼容适配器，再移动实现：

```text
标准化行情/账户/市场上下文
  -> platform_breakout.features
  -> platform_breakout.rules
  -> RuleResult 列表
  -> Decision Resolver
  -> 旧 preview/one-click 响应适配器
```

优先抽取纯计算的 `_direction` 和 `_platform_pattern`；规则版本持久化、API 字典、数据库保存和前端映射暂留兼容层。每一步必须保持本阶段 Golden Master 的合同与哈希不变。
