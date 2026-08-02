# Strategy Engine V2 重构质量基线

## 1. 目的

本基线用于区分“重构造成的回归”和“接管前已存在的技术债”。阶段 B 不全面修复 Ruff 或 mypy，不改业务代码。

## 2. 接管时基线

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 单元/集成测试 | `.venv/bin/python -m pytest` | 65 passed，0 failed，1 warning，30.86s |
| Ruff | `.venv/bin/python -m ruff check .` | 331 errors；101 可自动修复，未执行 `--fix` |
| mypy | `.venv/bin/python -m mypy app` | 41 个源文件；15 个文件共 62 errors |

唯一 pytest 警告来自 FastAPI `TestClient` 入口导入的 Starlette/httpx 弃用提示，不是测试失败。

## 3. Ruff 原因分类

主要存量问题：

- `I001`：导入块未排序。
- `B008`：FastAPI `Depends`、`File`、`Query` 在参数默认值调用；这是当前框架常见声明方式，但现有 Ruff 规则未配置例外。
- `DTZ005/DTZ011`：无时区 `datetime.now()` / `date.today()`。
- `BLE001`：宽泛捕获 `Exception`，主要用于 Provider 降级与流程兜底。
- `FURB157`：`Decimal("10")` 等写法被新 Ruff 版本视为冗长。
- 另有行长、复杂度、导入和风格问题。

这些问题分布在应用、迁移和测试中。批量 `ruff --fix` 会制造大面积无关 diff，不应与策略行为抽取混合。

## 4. mypy 原因分类

- pandas、pandas-ta-classic、vectorbt、backtesting、AKShare、twscrape、APScheduler 等缺少类型桩或 `py.typed`。
- 可选 Tushare Provider import 对应依赖未安装。
- SQLAlchemy 查询返回的 Optional 没有全部收窄。
- 纪律规则参数以 `object` 字典值传递，Decimal/float/int 运算类型不明确。
- `trade_plan_generator.py` 中 `SimpleNamespace` 代替 Holding、可空字典索引和可空规则版本导致多处错误。
- API CSV 解析和 Schema 默认值存在真实类型不匹配。

其中既有第三方噪声，也有真实领域类型缺失。应在 Strategy/RuleResult/Decision 领域类型建立后分批处理。

## 5. 阶段 B 新增保护

- 新增 10 个 Golden Master 场景测试。
- 每个场景分别保存输入 JSON、数据 fixture JSON、输出合同 JSON。
- 输出合同包含 Decision、计划状态、missing data、12 道 Gate、买入区、硬止损、仓位、风险金额、持仓状态和 AI 状态。
- 输出同时保存完整规范化结果的 SHA-256；时间戳、数据库 ID、请求耗时、哈希型运行标识不进入规范化结果。
- AI 关闭场景会与“AI 开启但未配置”的确定性结果对比，冻结规则不变性。

阶段 B 完成后的预期全量测试数为 75（原 65 + Golden Master 10）。

## 6. 后续治理原则

- 策略抽取提交不得顺带批量修 lint。
- Ruff 配置决策、机械格式化、第三方类型桩和真实类型修复应拆为独立提交。
- 每次领域重构先跑 Golden Master，再跑全量 pytest。
- 若确需更新 Golden 快照，必须解释业务差异，不能只替换 SHA-256。
