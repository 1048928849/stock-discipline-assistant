# Trade Plan Application Layer 架构

## 1. 当前生成流程

当前 `trade_plan_generator.py` 同时完成：数据库读取、规则版本准备、Feature/Strategy/Risk/Decision编排、价格计划计算、旧Gate和JSON组装、preview hash、正式计划保存、执行初始化、历史查询和版本比较。这使一次preview既有纯计算也可能有规则版本写入副作用。

现有调用链：

```text
API / OneClick / AI
  -> trade_plan_generator.generate_trade_plan_preview
  -> 数据库读取与规则版本准备
  -> Feature -> Strategy -> Risk -> Decision
  -> 价格计划与旧JSON组装
  -> preview hash
```

确认保存时，同一模块再次生成preview、校验hash、创建TradePlan/Checks、初始化execution并提交事务。

## 2. 新职责划分

| 模块 | 职责 | 禁止 |
|---|---|---|
| `trade_plan/application.py` | 读取应用所需数据并编排Feature、Strategy、Risk、Decision和Assembler | 保存正式计划 |
| `trade_plan/assembler.py` | 把已计算结果和展示段组合为内存TradePlanPreview | 数据库、Provider、业务计算 |
| `trade_plan/persistence.py` | 规则版本副作用、保存、历史、版本比较、快照持久化 | Feature/Strategy/Risk计算 |
| `trade_plan/lifecycle.py` | draft/preview/confirmed/executing/holding/closed领域边界和兼容映射 | 强制替换数据库execution状态 |
| `trade_plan/compatibility.py` | 原Gate结构、旧JSON字段、preview hash和兼容投影 | 策略、风险和决策计算 |
| `trade_plan_generator.py` | 旧导入路径兼容门面 | 完整业务流程实现 |

## 3. 目标数据流

```text
TradePlanPreviewRequest
  -> Application（准备纯值上下文）
  -> FeaturePipeline
  -> Strategy Engine
  -> Risk Engine
  -> Decision Engine
  -> Assembler（TradePlanPreview内存对象）
  -> Compatibility（旧API dict + hash）
```

保存流程：

```text
TradePlanSaveRequest
  -> Persistence
  -> Application重新生成preview
  -> 校验preview hash
  -> Lifecycle确认快照
  -> TradePlan + TradePlanCheck + execution初始化
  -> commit
```

## 4. Trade Plan领域模型

- TradePlanDraft：尚未形成稳定preview的内存草稿。
- TradePlanPreview：未保存的分析结果；用户确认前不创建正式TradePlan。
- TradePlanSnapshot：确认时冻结的preview和hash。
- TradePlanVersion：版本号与父版本概念。
- TradePlanLifecycle：draft、preview、confirmed、executing、holding、closed；只作为新领域边界，不改旧数据库状态。

模型使用冻结dataclass和只读Mapping，不包含ORM对象。

## 5. 行为兼容

- 用户确认前仍不保存正式计划。
- Preview仍可能准备并激活规则版本；阶段G只隔离副作用，不取消。
- confirm仍重新生成preview并校验hash，变化则返回409。
- 正式计划仍冻结engine/market/account/source snapshot。
- API字段、中文状态、Gate顺序、execution状态、历史和比较响应保持不变。
- `trade_plan_generator.py` 保留现有公开名称，调用方无需同步修改。

## 6. 剩余问题

- Application为兼容现有行为仍接收Session并读取数据；未来可通过Repository进一步隔离。
- 价格计划（止损价、目标价、买入区）尚未形成独立领域服务。
- 规则版本准备仍在preview路径提交事务。
- AI开启仍会重复执行确定性preview。
- 手工计划workflow仍有独立组装和持久化路径。
