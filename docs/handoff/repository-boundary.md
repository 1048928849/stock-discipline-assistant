# Trade Plan Repository 边界

## 1. 目标

阶段H让Trade Plan Application不再导入SQLAlchemy、ORM模型或直接调用`db.get/db.scalar/db.scalars/select`。Application只依赖`TradePlanReadRepository`协议，默认由SQLAlchemy适配器实现。

本阶段只迁移Application当前需要的读取和规则版本准备，不统一全项目数据库访问。

## 2. Repository接口

| 方法 | 返回 | 当前来源 | 是否包含业务判断 |
|---|---|---|---|
| `get_account(account_id)` | AccountRecord或None | Account | 否 |
| `get_holding(account_id, symbol)` | HoldingRecord或None | Holding | 否 |
| `get_holdings(account_id)` | HoldingRecord元组 | Holding | 否 |
| `get_company_profile(symbol)` | CompanyProfileRecord或None | CompanyProfile | 否 |
| `get_latest_bar(symbol)` | MarketBarRecord或None | MarketDailyBar | 否 |
| `get_quote(symbol)` | MarketQuoteRecord或None | MarketQuote | 否 |
| `load_qfq_frame(symbol)` | pandas DataFrame | MarketDailyBar | 否 |
| `ensure_rule_version()` | RuleVersionRecord | 现有Persistence规则版本逻辑 | 否；保留副作用 |

所有Record均为冻结dataclass，只包含Application使用的字段。Repository不会生成Gate、策略状态、风险结果或交易结论。

## 3. Adapter结构

```text
app/domain/repository/
  contracts.py
  models.py

app/services/repository/
  trade_plan_repository.py       # 默认Repository工厂

app/services/repositories/sqlalchemy/
  account_repository.py
  market_repository.py
  trade_plan_repository.py       # 组合适配器
```

AccountRepository负责账户和持仓；MarketRepository负责公司概况、行情元数据和K线Frame；SqlAlchemyTradePlanRepository组合二者并委托现有规则版本Persistence。

## 4. Application边界

`generate_trade_plan(db, request, repository=None)`为兼容旧调用继续接收Session，但函数体只把它交给默认Repository工厂。注入Repository时不使用Session，测试可用内存Fake验证完整调用链。

Application仍负责把Repository纯值记录组织成Feature/Strategy/Risk/Decision输入，但不执行查询语句。

## 5. 非目标

- Persistence保存、历史和比较暂不改成Repository类，现有边界已经集中。
- 不迁移OneClick、Workflow、AI和其他服务的ORM访问。
- 不引入Unit of Work或异步Repository。
- 不改变规则版本创建和commit时机。

## 6. 空数据语义

- 不存在账户返回None，由Application保持ACCOUNT_NOT_FOUND。
- 不存在持仓、公司资料、Bar或Quote返回None，沿用原降级路径。
- 无前复权K线继续抛出原ValueError文本，由Application转换为missing_data。
