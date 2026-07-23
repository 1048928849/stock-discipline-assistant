# 股票纪律助手

## 从股票代码开始：一键交易计划

首页现在提供普通用户的核心入口。只需输入 6 位A股代码并选择“空仓”或“已经持有”；系统会自动选择最近账户，没有账户时按默认 30 万元建立研究账户。持仓模式只需额外填写数量和成本。

点击“开始分析并生成计划”后，后端会独立刷新本交易日报价（不被日线缓存短路），再执行前复权日线检查、沪深300市场风险、行业板块相对强度、个股多周期与平台结构、账户风险和仓位、AI证据解释、统一计划输出。AI失败或未配置不会改变或阻断规则计划。

结果只使用“禁止买入、等待观察、允许试仓、允许持有、允许条件式加仓、建议减仓、计划失效需要退出”七类结论。未满足本交易日报价、止损、最低一手、确认顺序或追价范围时只能保存为观察计划；所有硬规则通过才标记为可执行计划。分析预览、步骤、数据来源、规则快照与AI审计会保留。

主要接口：

- `POST /api/v1/trade-plan-generator/analyze`：一键分析，不自动保存正式计划；
- `POST /api/v1/trade-plan-generator/analyze/{run_id}/confirm`：用户确认并冻结计划；
- `GET /api/v1/trade-plan-generator/analyze/{run_id}`：读取分析步骤与审计快照。

一个仅用于个人研究、交易记录与纪律提醒的 A 股辅助系统。它不预测股价、不提供“必买/必涨”结论，不连接券商，也不会自动下单。

首次使用请先阅读：[启动与环境说明](./启动与环境说明.md)。该文档列出了当前电脑已经具备和仍然缺少的运行环境。

## 已实现功能

- FastAPI 应用、中文首页和 `/api/v1` API；
- 账户与持仓 CRUD，金额/价格统一使用 `Decimal` / `Numeric`；
- 市值、浮盈亏、收益率和仓位计算，仓位以包含现金的账户总资产为分母；
- 任务书要求的 15 张基础表、唯一约束和 Alembic 初始迁移；
- SQLite 本地配置和 MySQL 8 Docker Compose 配置；
- 统一中文错误响应、输入校验、日志敏感信息脱敏；
- 抽象 `MarketDataProvider`，外部数据源不可用时不会影响应用启动。
- 可配置纪律规则：单股/板块/总仓位、止损、缺失理由、过度交易、计划外和情绪化交易、下跌后短期加仓风险；
- 交易记录、UTF-8 CSV 导入去重、单笔结论以及日/周复盘指标；
- AKShare 股票列表、实时行情、前复权日线、指数、行业板块与公告适配器；实时价按东方财富→新浪、历史线按东方财富→腾讯→新浪自动降级；
- twscrape 关注账号/查询管理、低频增量采集和帖子 ID 去重；
- OpenAI-compatible 翻译、摘要、分类、证据性质与待验证项，Pydantic 严格校验并重试一次；
- “交易规则历史验证”页面：固定止损、双均线、分批买入和趋势过滤，明确买入/卖出/重入规则，并与中文基准“买入并持有”对比；
- 回测展示总收益、年化收益、最大回撤、胜率、盈亏比、交易次数、持仓天数、资金/回撤曲线和逐笔模拟交易；默认至少 3 年，少于 30 笔统一标记“样本不足”；
- AKShare 前复权 K 线、pandas-ta-classic 指标、均线排列/MACD 背离/放量突破/缩量回调识别；
- 支撑压力区间、指标矛盾解释、vectorbt 逐信号历史胜率与收益验证；
- 持仓每日技术状态快照和变化记录；
- “公司研究中心”：最近 12 个季度三大财务报表、单季度趋势、财务质量解释和异常指标；
- 巨潮资讯公告目录（失败时回退东方财富公开公告聚合），按证券市场标记上交所/深交所，自动分类年报、业绩预告、减持、质押、诉讼、监管问询、担保、解禁和审计意见，并形成红黄灰风险雷达；目前尚未直接接入上交所、深交所各自的独立公告接口；
- PE、PB、PS 历史分位、同行估值/经营指标对比，以及乐观/中性/悲观隐含利润增长情景；
- 主营业务、上下游、客户、产能、研发和竞争优势证据提取，并与已采集 X 资讯组成分类时间线；
- P0 研究交易闭环：今日工作台、候选池与事前交易计划、风险仓位计算、七项计划检查、持仓纪律状态与复盘汇总；
- “交易计划生成器”：输入股票与账户后，确定性识别平台、放量突破、缩量回踩和再次转强，输出 `READY / WAIT / NO_TRADE / INSUFFICIENT_DATA`、13 道闸门（含本交易日报价闸门）、条件式买入区、硬止损、风险收益、确认加仓和退出纪律；
- 仓位同时受风险预算、可用资金、单股上限、账户总仓位和行业集中度约束，最终向下取 100 股整手；已有持仓会检查浮盈确认加仓、硬止损和第一减仓触发；
- 计划预览不自动保存；用户确认后冻结行情、账户、规则、证据和参数快照，并明确保存为 `watch` 或 `executable`。重新计算创建新版本，可查询历史并比较差异；
- 大模型辅助研究采用独立证据包和固定 Schema，只归纳公司、财报、产业、估值、支持/反方证据；引用会校验股票和 `source_id`，输出不能覆盖规则状态、仓位或硬止损。未配置、超时或校验失败时规则计划仍正常工作；
- 规则参数版本化：默认单笔风险 1%、单只仓位上限 25%、账户回撤阈值 8%，均可创建新版本调整，旧计划保留原版本；
- A 股数量同时受风险预算、仓位上限、可用现金和 100 股整手约束；硬止损触发后不允许通过评估放宽止损或继续加仓；
- 行情写入前执行完整性检查，统一记录 `trading_date / quote_time / market_status / price_type / provider_id / data_as_of`；失败时可展示最近成功数据并明确标记过期，但不会将最新日线收盘写成当前报价；
- 市场、复盘、技术、X、设置和回测页面的原始响应统一放入折叠技术详情；
- APScheduler 日/周复盘和 15 分钟 X 同步，可通过配置启用；
- 仪表盘、持仓、交易、复盘、市场、X、回测和设置中文页面。

外部服务失败时会返回明确状态，不会用随机数或硬编码数据冒充行情、帖子或 AI 结果。

“交易规则历史验证”会显示初始资金、仓位、手续费、滑点、复权方式和基准定义。原始 JSON 仅保留在默认折叠的“开发者详情”中。当前仍未严格模拟 T+1、涨跌停、停牌和成交量限制，页面会逐项标明。

## Windows 本地启动

需要 Python 3.11 或 3.12。在 PowerShell 中运行：

```powershell
.\start.ps1
```

脚本会创建 `.venv`、安装依赖、运行迁移并只监听 `127.0.0.1:8000`。访问：

- 首页：http://127.0.0.1:8000/
- API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health

如已安装依赖，可用 `.\start.ps1 -SkipInstall`。也可手工执行：

```powershell
python -m pip install -e ".[dev]"
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Docker + MySQL

复制 `.env.example` 为 `.env`，设置强密码后执行：

```powershell
docker compose up --build
```

容器内应用监听 `0.0.0.0:8000`。不要在公网暴露未加认证的首版服务。当前开发机未安装 Docker CLI，因此配置已提供但尚未在本机实跑。

## 配置

所有配置均可通过 `.env` 注入。不要提交 `.env`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./stock_assistant.db` | SQLite 或 SQLAlchemy MySQL URL |
| `APP_HOST` | `127.0.0.1` | 本地监听地址 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 空 | OpenAI-compatible AI 辅助研究；三项需同时配置 |
| `LLM_ENABLED` | `true` | 是否启用 AI 辅助研究；关闭后不影响规则计划 |
| `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | `30` / `1` | 模型超时与校验失败重试 |
| `LLM_MAX_INPUT_CHARS` / `LLM_DAILY_LIMIT` | `40000` / `20` | 证据包长度与每日调用上限 |
| `LLM_CACHE_HOURS` | `24` | 相同股票、模型和证据版本的缓存期限 |
| `X_COOKIE` | 空 | X 公开信息采集；日志和设置页不回显值 |
| `SCHEDULER_ENABLED` | `true` | 日/周复盘、持仓技术快照和 X 定时任务 |
| `X_SYNC_MINUTES` | `15` | X 低频同步周期 |

Gemini 可执行 `./configure-gemini.ps1`，脚本会以隐藏输入读取 API Key、验证模型后写入本机 `.env`。当前默认使用 `gemini-3.6-flash` 和 Google 官方 OpenAI-compatible 地址；也可以在设置页点击“测试大模型连接”。

## API 示例

创建账户：

```json
POST /api/v1/accounts
{
  "name": "主账户",
  "total_assets": "100000.0000",
  "cash": "60000.0000",
  "available_cash": "55000.0000"
}
```

创建持仓：

```json
POST /api/v1/holdings
{
  "account_id": 1,
  "symbol": "600519",
  "name": "贵州茅台",
  "quantity": 10,
  "cost_price": "1500.0000",
  "current_price": "1600.0000",
  "price_source": "manual"
}
```

## 技术研究工作流

1. 在“市场数据”页同步股票，系统保存 AKShare 前复权（qfq）日线；
2. 在“技术研究”页输入股票代码，查看当前价格、分析日期、日线周期、MA20/MA60、MACD、RSI、量比、支撑压力和指标冲突；
3. 点击“回测各项信号”，vectorbt 分别统计均线多头形成、MACD 金叉、放量突破、缩量回调和 MACD 底背离；
4. 结果展示样本量、持有期胜率、平均/中位收益、组合收益、最大回撤和同期基准；
5. 每个交易日 16:10，系统刷新持仓复权日线并生成技术状态快照。刷新失败时不会用旧数据冒充当日快照。

相关 API：

- `GET /api/v1/technical/{symbol}`
- `POST /api/v1/technical/{symbol}/backtest`
- `POST /api/v1/technical/holdings/snapshots?refresh_market=true`
- `GET /api/v1/technical/holdings/changes`

支撑压力按 ATR 限制区间宽度；连续 K 线触碰合并为一次独立测试，并以约 90 个交易日半衰期降低旧位置权重。完整位于现价下方的归为支撑、位于现价上方的归为压力，跨越现价的自动标为“震荡/争夺区”。页面同时给出突破确认、跌破确认和各自失效条件。这些位置不是保证有效的精确价格。

## 公司研究中心工作流

1. 打开 `/company-research`，输入六位 A 股代码；
2. 点击“同步并生成研究报告”，系统读取最近 12 个季度财务数据、近三年公告及历史估值；
3. 按需勾选“解析最新年报原文”。PDF 较大时会明显增加同步时间；
4. 查看经营/财务趋势图、异常解释、公告风险雷达、同行比较、估值情景和产业证据；
5. 每张结论卡显示报告期、来源与更新时间，风险事项可跳转公告原文；原始响应只在“开发者详情”中显示。

相关 API：

- `POST /api/v1/company-research/{symbol}/sync?include_documents=true`
- `GET /api/v1/company-research/{symbol}`

估值分位依赖可获得的历史样本；PS 历史序列由历史市值与最近四个单季度营收推导。同行公司的报告期可能不完全一致，页面会明确提示。产业结论只组织已有公司资料、公告、年报段落和已采集 X 资讯，不会用 AI 填补缺失事实。

## 交易计划生成器与持仓纪律

打开 `/trade-plans` 后只需输入股票代码、选择空仓或持仓；系统自动检查并刷新前复权日线、公司概况、最近12季度财务、估值、公告和风险事件，再计算沪深300环境、行业相对强度、技术结构和账户风险。外部源失败时保留最近成功缓存并标记；没有缓存时列入 `missing_data`。

`可买数量 ≈（账户权益 × 单笔风险比例）÷（计划买入价 − 初始止损价）`

生成结果显示 12 道闸门和四种明确状态。`READY` 只代表规则条件齐备；`WAIT` 表示等待突破/回踩/转强；`NO_TRADE` 表示存在硬性不通过；`INSUFFICIENT_DATA` 表示关键数据不足。页面不会把评分包装成买入建议。

点击“确认并保存当前版本”后才会创建正式计划；行情、规则版本、账户权益、风险参数、买入区、止损、来源和判断证据会冻结。点击“运行 AI 辅助研究”是第二阶段：没有配置模型时显示“未配置AI分析”，不影响确定性结果。

相关 API：

- `POST /api/v1/trade-plan-generator/preview`
- `POST /api/v1/trade-plan-generator/save`
- `GET /api/v1/trade-plan-generator/history`
- `GET /api/v1/trade-plan-generator/compare`
- `POST /api/v1/trade-plan-generator/holding-check`
- `GET /api/v1/trade-plan-generator/rules`
- `POST /api/v1/trade-plan-generator/ai`
- `POST /api/v1/trade-plan-generator/analyze`
- `POST /api/v1/trade-plan-generator/analyze/{run_id}/confirm`
- `GET /api/v1/trade-plans/{plan_id}/execution`
- `POST /api/v1/trade-plans/{plan_id}/execution/fills`
- `POST /api/v1/trade-plans/{plan_id}/execution/evaluate`
- `GET /api/v1/data-sources/status`

AI响应采用 `schema_version=2.0` 的严格结构，后端冻结 `computed_results`、`raw_facts`、`rule_conclusions`、`data_freshness` 和 `provider_status`；模型只能填写证据归纳、推断、支持/反方证据、冲突和风险。模型修改规则字段、引用不存在或其他股票的 `source_id`、生成证据中不存在的数字时，分析会被拒绝并自动重试一次。AI失败不影响规则计划。

正式计划确认后可手工录入成交。系统记录 `waiting_entry`、`entry_triggered`、`partially_executed`、`holding`、`add_triggered`、`reduce_triggered`、`stop_triggered`、`take_profit_triggered`、`invalidated`、`closed` 等状态，并检查超仓、亏损补仓、未满足条件买入和未执行止损。系统不连接券商、不自动下单。

数据访问统一经过Provider注册和路由层。当前免费源为AKShare；Tushare、标准专业行情HTTP API、标准新闻HTTP API和X线索Provider均支持启用/禁用、能力与凭据检测，未配置时安全跳过。Provider按配置统一重试；日线业务只接受标准化的 `frequency=daily, adjustment=qfq` 数据，不再按供应商名称写死判断。

## 数据库迁移与备份

升级到最新结构：`python -m alembic upgrade head`。回退一个版本：`python -m alembic downgrade -1`。

SQLite 停服后可直接复制 `stock_assistant.db` 备份。MySQL 建议使用：

```powershell
docker compose exec mysql mysqldump -uroot -p stock_assistant > backup.sql
Get-Content .\backup.sql | docker compose exec -T mysql mysql -uroot -p stock_assistant
```

密码由命令交互输入，不要将密码写进脚本或终端历史。

## 测试

```powershell
python -m pytest
python -m ruff check app tests
```

测试使用内存 SQLite，不访问实时行情、不需要 Cookie，也不产生 LLM 费用。

## 常见问题

- `No module named ...`：先执行 `python -m pip install -e ".[dev]"`。
- 数据库连接失败：检查 `DATABASE_URL`；MySQL URL 中的特殊字符须进行 URL 编码。
- 端口被占用：手工启动时将 `--port 8000` 改为其他端口。
- 外部数据源不可用：接口会明确显示失败状态，不会伪造行情或静默使用过期数据。
- 东方财富出现 `ProxyError`：无需关闭代理，市场同步会自动切换到新浪实时价和腾讯/新浪前复权日线；修改代码后须重启 Uvicorn 才会生效。

## 安全与数据风险

公开行情和社交平台接口可能变更、延迟或限流，不能作为交易成交依据。Cookie、API Key、数据库密码只放在本机 `.env` 中。系统不会执行外部内容包含的命令。当前阶段尚未提供身份认证，只适合在本机或受信任网络使用。

## 第三方组件评估

- FastAPI（MIT）、SQLAlchemy（MIT）、Alembic（MIT）、Pydantic（MIT）、Uvicorn（BSD-3-Clause）。
- AKShare 1.18.x（MIT）：通过适配器接入，避免业务层直接依赖。
- twscrape 0.19.x（MIT）：低频接入，仅使用用户有权访问的公开内容与自有凭据。
- backtesting.py 0.6.x（AGPL-3.0）：作为依赖调用，不复制源码；外部分发或提供网络服务前需履行 AGPL-3.0 义务。
- RQAlpha（Apache-2.0）：首版不引入，只有组合回测制度模拟确有需要时再评估。
