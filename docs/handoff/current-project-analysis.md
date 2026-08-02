# 股票策略分析纪律助手：当前项目分析

## 1. 分析范围与基线

- 仓库：`1048928849/stock-discipline-assistant`
- 当前分支：`main`
- 远端：`origin = https://github.com/1048928849/stock-discipline-assistant.git`
- 工作区初始状态：干净，`main` 与 `origin/main` 同步，无需 stash
- 当前提交：`e30e6e7 feat: complete one-click research and plan execution phase`
- 本文档只恢复开发上下文，不包含策略重构或业务功能变更。

最近提交：

```text
e30e6e7 feat: complete one-click research and plan execution phase
d0d5d1e Ignore SQLite runtime files
e636e22 Initial research and discipline console
```

## 2. 运行环境与质量基线

### 2.1 环境

| 项目 | 当前状态 |
| --- | --- |
| Python | `python` 为 Miniconda Python 3.13.13；项目声明 `>=3.11`，README 推荐 3.11/3.12 |
| Node.js | 24.15.0 |
| SQLite | 3.51.0，可用；`.env.example` 默认 `sqlite:///./stock_assistant.db` |
| MySQL | 已安装 Homebrew `mysql@8.4` 8.4.11；未启动服务、未设 root 密码、未创建项目库 |
| Docker | 未安装；仓库包含 `Dockerfile` 和 MySQL 8.4 `docker-compose.yml` |
| Python 虚拟环境 | `.venv`，已执行 `pip install -e ".[dev]"` 且成功 |

MySQL Compose 使用 `stock_assistant` 数据库和 `stock_user` 用户；密码来自环境变量。当前本地默认运行路径仍是 SQLite。

### 2.2 测试与静态检查

```text
pytest
测试数量：65
通过：65
失败：0
警告：1（FastAPI TestClient 引用的 Starlette/httpx 弃用警告）

ruff check .
通过：否
错误：331
可自动修复：101（未执行 --fix）
主要类别：导入排序、FastAPI Depends/File/Query 默认调用、无时区 datetime、宽泛异常捕获、Decimal 写法等

mypy app
检查文件：41
通过：否
错误：62（15 个文件）
主要类别：第三方包缺少类型桩、Optional 未收窄、Decimal/对象类型不匹配、可空字典索引、缺失 tushare 包
```

测试全部通过，但 Ruff 和 mypy 尚未形成可用于 CI 的绿色基线。未删除测试、未更改依赖约束、未为了通过检查修改业务代码。

## 3. 当前目录结构

```text
app/
├── api/                    # FastAPI 路由；基础 CRUD、高级功能、技术、公司研究、工作流
├── providers/              # 行情、基本面、公告、新闻、社交和 LLM Provider 适配器及注册表
├── services/               # 业务编排、计划生成、执行审计、研究、回测、技术指标和纪律检查
├── static/                 # 单页式前端 CSS/JavaScript；包含状态标签和部分规则展示文案
├── templates/              # Jinja2 页面壳，目前主要是 index.html
├── config.py               # Pydantic Settings 环境配置
├── database.py             # SQLAlchemy Engine、Session、Base
├── models.py               # 账户、数据、研究、策略计划、AI、执行等全部 ORM 模型
├── schemas.py              # 基础 API Schema
├── schemas_advanced.py     # 交易、纪律、回测等 Schema
├── schemas_workflow.py     # 计划、规则、AI、一键分析和执行 Schema
├── scheduler.py            # 日/周复盘、技术快照、X 同步等计划任务
└── main.py                 # 应用工厂、页面路由、静态资源和 API Router 注册

alembic/
├── env.py
└── versions/               # 8 个迁移，逐步增加基础表、研究、计划、一键流程、AI 与执行审计

tests/                      # 65 个基线测试，覆盖 API、组合、技术、研究、Provider、计划与一键流程
```

当前没有 `app/domain` 或独立 `strategies` 包。领域模型、持久化模型、规则实现和流程编排尚未分层。

## 4. 核心入口与完整调用链

入口为：

```text
POST /api/v1/trade-plan-generator/analyze
```

实际调用链：

```text
app/main.py:create_app
  -> app/api/workflow.py:analyze_and_generate_plan
  -> app/services/one_click_pipeline.py:run_one_click_analysis
       -> _default_account
       -> UnifiedDataService
       -> _sync_stock
       -> refresh_company_research_if_needed
       -> _ensure_profile
       -> _market_assessment
       -> _sector_assessment
       -> _research_inventory
       -> TradePlanPreviewRequest
       -> generate_trade_plan_preview
            -> ensure_generator_rule_version
            -> load_qfq_frame
            -> prepare_indicators
            -> _platform_pattern
            -> 12 道 gate 构造
            -> 止损、盈亏比和仓位计算
            -> READY / WAIT / NO_TRADE / INSUFFICIENT_DATA
       -> _decision
       -> run_ai_analysis（可关闭或失败降级）
            -> 重新生成确定性 preview 并核对 preview_hash
            -> build_evidence_package
            -> OpenAICompatibleProvider.analyze_trade_plan
            -> validate_ai_output / TradePlanAIResult Schema
       -> 保存 PlanAnalysisRun 审计快照
       -> 返回 decision、plan、steps、AI 和保存条件
```

注意：当前实现先生成确定性结论，再调用 AI 解释；AI 不是 Decision Resolver 的输入。

确认保存链路：

```text
POST /api/v1/trade-plan-generator/analyze/{run_id}/confirm
  -> confirm_one_click_plan
  -> save_generated_plan
  -> 再次生成 preview 并校验 preview_hash
  -> 保存 TradePlan / TradePlanCheck / 冻结快照
  -> initialize_plan_execution
  -> 保存 PlanExecutionEvent
```

分析预览本身不会创建正式计划；但在无账户时会创建“默认研究账户”，行情/研究同步也会写入缓存与调用审计日志。

## 5. 当前已经实现的能力

### 5.1 账户与持仓

- 账户 CRUD：总资产、现金、可用现金。
- 持仓 CRUD：股票、数量、成本、当前价格、止损、目标、行业。
- 组合计算：市值、浮盈亏、收益率、持仓占比。
- 风险预算：单笔风险、单股上限、账户总仓位、行业集中度、可用现金与 A 股 100 股整手共同约束。
- 无账户时，一键流程可按默认 30 万元创建研究账户。

### 5.2 数据与研究

- 股票列表、实时行情、前复权日线、宽基指数、行业板块。
- 公司概况、最近 12 个季度财务、公告、估值、公司研究证据。
- 技术指标、多周期方向、支撑压力、信号历史验证、持仓技术快照。
- X 线索、可配置专业行情/新闻 HTTP Provider、Tushare Provider 骨架。
- Provider 注册、优先级、健康状态、失败降级、本地缓存回退和调用日志。

ORM 当前包含 29 个业务表模型，覆盖账户、持仓、交易、复盘、纪律、行情、研究、规则版本、计划、AI、一键分析与执行事件。

### 5.3 当前策略/规则能力

存在两类“策略”概念：

1. 回测演示策略，位于 `app/services/backtests.py`：
   - 固定止损 `fixed_stop`
   - 双均线交叉 `moving_average`
   - 分批买入与止盈止损 `batch_buy`
   - 趋势过滤 `trend_filter`

2. 生产交易计划策略：
   - 名称：`日线趋势波段：平台放量突破—缩量回踩—再次转强`
   - 前端简称：`平台突破—回踩确认`
   - 核心实现：`app/services/trade_plan_generator.py`
   - 参数：`GENERATOR_PARAMETERS`
   - 策略说明和硬性禁止项：`GENERATOR_RULES`
   - 特征/结构识别：`_direction`、`_platform_pattern`，并复用 `technical.prepare_indicators`
   - 规则闸门、风险收益、仓位、持仓加减仓和最终状态也全部位于同一文件。

当前只有这一套生产计划生成策略；回测策略没有接入一键计划的统一策略接口。

### 5.4 计划、纪律与审计

- 计划预览、确认保存、版本历史、版本比较和规则版本快照。
- 手工成交录入，不连接券商、不自动下单。
- 检查 100 股整手、未触发买入、买入区偏差、亏损补仓、超仓、超卖和漏执行止损。
- 记录执行事件、执行状态、已实现盈亏/R、规则执行率和计划完整率。
- 日/周复盘、纪律规则和纪律事件模型已存在。

## 6. 当前架构问题

### 6.1 策略代码耦合

`平台突破—回踩确认` 尚未成为独立策略模块：

- 定义、默认参数、版本升级、副作用式数据库写入都在 `trade_plan_generator.py`。
- 特征计算混合使用 `technical.py` 和 `_platform_pattern`。
- 规则闸门、风险计算、仓位计算、状态解析、计划序列化与数据库保存位于同一 service。
- `one_click_pipeline.py` 额外决定市场仓位上限，并把内部 `preview.status` 再映射为用户结论。
- `workflow.py` 仍保留旧版规则与计划状态逻辑。
- `app/static/app.js` 固化状态标签、策略文案和部分规则说明。
- `schemas_workflow.py` 固化当前唯一交易模式和输入状态。
- `models.py` 使用自由字符串保存策略、规则和执行状态，没有领域枚举约束。

因此未来抽离时必须先用 Golden Master 固定 `generate_trade_plan_preview` 和一键接口输出，避免跨层重构造成行为漂移。

### 6.2 状态体系混乱

当前至少存在以下状态空间：

| 状态域 | 当前值 |
| --- | --- |
| 计划生成内部状态 | `READY`、`WAIT`、`NO_TRADE`、`INSUFFICIENT_DATA` |
| 旧工作流计划状态 | `DRAFT`、`READY`、`BLOCKED`；部分查询同时接受这些值 |
| 用户决策状态 | `BUY_PROHIBITED`、`WAIT`、`TRIAL_ALLOWED`、`HOLD`、`CONDITIONAL_ADD`、`REDUCE`、`PLAN_INVALID_EXIT` |
| 执行状态 | `draft`、`confirmed`、`waiting_entry`、`entry_triggered`、`partially_executed`、`holding`、`add_triggered`、`reduce_triggered`、`stop_triggered`、`take_profit_triggered`、`invalidated`、`closed` |
| Gate/规则状态 | 中文 `通过`、`警告`、`不通过`、`无法判断` |
| 一键运行状态 | `running`、`success`、`failed`、`confirmed` |
| Pipeline step 状态 | `success`、`partial`、`failed`、`skipped` |

目标七结论中的 `EXIT` 当前叫 `PLAN_INVALID_EXIT`；规则结果也尚未采用统一 `PASS / FAIL / WAIT / UNKNOWN`。`TradePlan.status`、`TradePlanCheck.status` 和前端均是自由字符串，容易出现不可追踪的新值。

### 6.3 数据、特征与规则耦合

积极部分：

- `UnifiedDataService` 已是业务层统一外部数据入口。
- Provider 通过 capability 路由，AKShare/Tushare/专业 HTTP 等可替换并有审计。
- `trade_plan_generator.py` 没有直接 import AKShare/Tushare Provider。

仍需收敛的部分：

- 规则生成器直接依赖 SQLAlchemy `Session`、ORM 模型和数据库查询。
- 规则直接读取 `MarketDailyBar`/`MarketQuote`/`Holding`，并接收 pandas DataFrame。
- `_platform_pattern` 同时计算特征和判断规则；标准化 Feature DTO 尚不存在。
- 一键编排层直接计算市场、行业状态和市场风险下的总仓位上限。
- `ensure_generator_rule_version` 在评估过程中可能写数据库并切换活动规则版本。

结论：外部供应商已大体隔离，但“数据获取/持久化 → 标准特征 → 策略规则”之间仍未隔离。V2 应让规则只依赖稳定、可序列化的标准化特征和账户风险上下文。

### 6.4 AI 权限边界

当前边界总体符合目标：

- 规则 preview 在 AI 调用前生成。
- AI 证据包冻结 `schema_version`、`computed_results`、`raw_facts`、`rule_conclusions`、`data_freshness`、`provider_status`。
- `validate_ai_output` 逐字段核对冻结内容，并禁止状态、买入价、止损、数量、仓位等键。
- AI 输出通过 `TradePlanAIResult` 严格 Schema 校验，并核对股票和 `source_id`。
- AI 未配置、失败或验证失败不会更改或阻断确定性规则计划。
- 保存时重新生成 preview 并校验 `preview_hash`，AI 记录也必须匹配股票与证据版本。

未发现 AI 修改买入结果、仓位、止损或规则的有效路径。后续重构仍应保留“确定性结果先生成、AI 只解释、失败可忽略”的单向依赖。

## 7. 迁移到 Strategy Engine V2 的约束

第一步不应新增策略，而应为当前生产策略建立行为基线，然后分层抽离：

1. 固定当前 preview、一键 decision、持仓判断和保存快照的 Golden Master。
2. 将 `_platform_pattern` 识别出的原始指标整理为标准化 Feature 输入。
3. 将策略定义、规则和特征移入独立 `strategies/platform_breakout`，由兼容适配层保持旧 API 输出。
4. 引入统一 `Strategy`、`StrategyVersion`、`StrategyRule`、`RuleResult`、`Decision` 领域类型。
5. 在兼容层统一旧状态与 V2 七结论，最后再迁移数据库自由字符串和前端映射。

优先风险：

- `generate_trade_plan_preview` 兼具读取、计算、规则版本写入和序列化职责，直接搬迁风险高。
- 一键入口会写缓存、默认账户和分析记录，Golden Master 需控制这些副作用。
- 现有 65 个测试全部通过，但未覆盖任务书要求的全部七结论及统一规则结果四状态。
- Ruff/mypy 基线很大，不宜与策略抽离混在同一提交中修复，否则难以确认业务行为是否保持。

## 8. 当前结论

项目已具备真实数据接入、确定性平台突破计划、账户风险与仓位、计划冻结、执行审计和受限 AI 解释的完整雏形。主要问题不是功能缺失，而是领域边界不清：生产策略集中在大型 service 内，状态体系并存，规则依赖数据库/DataFrame，前端又重复解释状态。V2 的首要工作应是用 Golden Master 锁定现有行为，再做策略模块抽离和状态统一。
