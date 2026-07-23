# 测试与交付验证报告

验证日期：2026-07-23
验证环境：Linux、Python 3.12、SQLite

## 第一阶段可信度修复

- 一键分析每次单独刷新报价，日线缓存不再跳过报价请求。
- 报价保存交易日、报价时间、市场状态、价格类型、Provider和数据时点；昨收不再冒充当前价格。
- 日线Provider使用统一 `daily/qfq` 契约；Tushare通过复权因子生成前复权数据，专业HTTP Provider会拒绝非前复权历史。
- 规则引擎新增本交易日行情闸门；盘中价格、收盘确认顺序、最大追价、今日涨停可达性、8%止损和最低100股共同决定是否允许立即执行。
- 买入区下沿不再低于触发价；确认发生在当天收盘时，最早从下一交易日执行。
- 计划明确分为 `watch` 和 `executable`，并持久化 `can_execute` 与阻塞原因。数量为0、旧报价或硬规则未通过时不会显示为可执行计划。
- 其他账户持仓会尝试刷新本交易日价格；缺失时账户总仓位与行业集中度闸门改为无法判断。
- 修复无效止损导致 `None > maximum_stop_distance_pct` 的运行时异常。
- 修复 `20260723_0008` 降级时未先删除 `execution_status` 索引的问题。

## 自动测试

- 命令：`python -m pytest -q --cov=app --cov-report=term-missing`
- 结果：70 passed
- 应用代码覆盖率：79%
- 测试不访问真实行情、不需要Cookie或LLM费用。

新增覆盖包括：

- 日线缓存新鲜时仍刷新本交易日报价；
- 旧报价只能生成观察计划；
- 买入区和触发顺序；
- 异常止损边界安全拦截；
- Provider统一重试；
- AI未配置时规则降级；
- Provider失败后的缓存降级；
- Provider和缓存均不存在时的明确 `missing_data`。

## 静态检查与迁移

- `ruff check .`：通过。
- `alembic upgrade head`：通过，当前Head为 `20260723_0009`。
- `alembic check`：无待生成迁移。
- `alembic downgrade -1 -> upgrade head`：通过。
- `alembic downgrade 20260723_0007 -> upgrade head`：通过，覆盖 `0008` 的回滚修复。
- `mypy app`：仍有60项历史类型错误；修改前审查记录为62项。本阶段没有把第三方无类型声明和既有模块类型债务扩大。

## 外部配置与降级

- 免费行情默认使用AKShare。
- `TUSHARE_ENABLED=true` 且配置 `TUSHARE_TOKEN` 后启用Tushare；未配置时安全跳过。
- `PROFESSIONAL_MARKET_API_ENABLED=true`、`PROFESSIONAL_MARKET_API_URL`、`PROFESSIONAL_MARKET_API_KEY` 用于标准专业行情Provider。
- `NEWS_API_ENABLED=true`、`NEWS_API_URL`、`NEWS_API_KEY` 用于新闻Provider。
- LLM未配置或校验失败时，确定性规则计划继续生成。
- 报价获取失败时可显示缓存或最近正式收盘，但会标记非本交易日行情并阻止立即执行。

## 尚未纳入本阶段

- 60分钟行情、日线换手率和盘后市场宽度；
- 行业、概念和产业链映射增强；
- 服务端真实任务进度；
- 自动盘中/盘后计划监控；
- 登录与权限系统。
