# Feature Engine 设计与阶段 C 交接

## 1. 目标与边界

Feature Engine 只回答“市场数据中已经发生了什么”，不回答“可以买还是不能买”。本阶段把平台、指标和多周期事实从交易判断中剥离，同时保留原策略参数、规则顺序、仓位公式、API 响应和数据库结构。

当前兼容链路为：

```text
MarketDailyBar
  -> load_qfq_frame
  -> FeaturePipeline
       -> platform_structure
       -> technical_indicators
       -> multi_timeframe
  -> FeatureSnapshot
  -> trade_plan_generator 旧规则兼容层
  -> 原有 Gate / 止损 / 仓位 / Decision
```

Golden Master 的合同和完整规范化结果哈希保持不变。

## 2. Feature 领域设计

领域类型位于 `app/domain/features/`。

### FeatureQuality

```text
GOOD
STALE
FALLBACK
PARTIAL
MISSING
CONFLICTED
```

质量只描述事实数据的可用性，不等价于规则 `PASS/FAIL/WAIT/UNKNOWN`，也不产生交易结论。

### FeatureValue

承载事实值及其证据元数据：

```python
FeatureValue(
    value=...,
    source_ids=(...),
    data_time="...",
    quality=FeatureQuality.GOOD,
)
```

### Feature

由稳定的 `feature_id` 和一个 `FeatureValue` 组成。序列化结构为：

```json
{
  "feature_id": "platform_structure",
  "value": {},
  "source_ids": ["akshare_tencent_qfq"],
  "data_time": "...",
  "quality": "GOOD"
}
```

### FeatureSnapshot

一个股票在指定日期的事实集合：

```json
{
  "symbol": "300308",
  "as_of": "2026-08-01",
  "features": []
}
```

Snapshot 保证 `feature_id` 唯一，支持按 ID 获取 Feature 或值。它不是数据库模型，本阶段不持久化。

## 3. Pipeline 输入输出

实现位于 `app/services/features/pipeline.py`。

输入：

- `symbol`：股票代码；
- `as_of`：事实截止日期；
- `market_data`：标准 OHLCV pandas DataFrame；
- `parameters`：当前活动策略参数的只读副本；
- `source_ids`、`data_time`、`quality`：数据来源与质量；
- `missing_reason`：行情缺失时的明确原因。

输出固定包含三个 Feature：

| feature_id | 事实内容 |
| --- | --- |
| `platform_structure` | 平台上下沿、区间、突破、突破量比、回踩、破位、转强触发等 |
| `technical_indicators` | MA5/20/60/250、MACD、RSI14、ATR14、量比、技术趋势描述 |
| `multi_timeframe` | 日线、周线、月线状态及大周期合并描述 |

行情缺失时仍返回三个 Feature，但质量为 `MISSING`，value 只包含缺失原因。

## 4. 当前抽取模块

### 平台事实

`app/features/platform.py:calculate_platform_facts`

从原 `trade_plan_generator._platform_pattern` 原样迁移：

- 平台观察周期；
- 平台上沿、下沿和区间振幅；
- 突破候选和突破量比；
- 回踩范围、回踩量比和缩量事实；
- 平台是否被破坏；
- 再次转强触发价及是否发生；
- 最新 MA、MACD、RSI、ATR 和量比兼容字段。

函数不返回 `READY/WAIT/NO_TRADE`，不计算仓位，不输出买卖结论。`valid_platform`、`volume_confirmed`、`turned_stronger` 是现有参数下的事实分类，为保持旧行为暂时保留。

### 技术指标事实

`app/features/technical.py:calculate_technical_facts`

复用现有 `prepare_indicators`，统一输出：

- MA5、MA20、MA60、MA250；
- MACD；
- RSI14；
- ATR14；
- 当前成交量 / VOL_MA20；
- `向上/震荡/向下` 技术状态描述。

不输出是否允许交易。

### 多周期事实

`app/features/timeframes.py`

从原生成器迁移 `_direction`，输出日、周、月三个时间框架的收盘、均线和方向描述，以及原有 `large_state` 合并事实。

## 5. 兼容接入

`generate_trade_plan_preview` 仍负责读取账户、持仓、行情和规则版本。读取标准化 K 线后，它调用 `FeaturePipeline.build`，再从 Snapshot 取出：

```text
platform_structure -> 原 pattern 字典
multi_timeframe     -> 原 daily/weekly/monthly/large_state
```

后续 Gate、止损、目标、仓位、`READY/WAIT/NO_TRADE/INSUFFICIENT_DATA` 和 API 字典未改。`technical_indicators` 已进入 Snapshot，但旧规则仍从 platform 兼容字段读取部分指标，以避免本阶段产生行为变化。

## 6. 测试

新增 `tests/features/test_feature_pipeline.py`，覆盖：

1. 正常平台；
2. 无法形成有效平台；
3. 数据不足和 `MISSING` 质量；
4. 突破成交量与量比；
5. 均线计算；
6. ATR 计算及平台/指标一致性；
7. 日/周/月多周期状态。

阶段 C 验证结果：

```text
Feature tests: 7 passed
Golden Master: 10 passed
Full pytest: 82 passed
Warnings: 1 existing third-party deprecation warning
```

## 7. 尚未抽取的逻辑

以下仍留在旧 service，属于下一阶段或兼容层职责：

- SQLAlchemy 查询和 `load_qfq_frame` 数据装配；
- 策略参数版本的读取、自动升级和事务提交；
- 12 道 Gate 的规则判断和中文证据文案；
- 市场/行业状态计算及高风险总仓位上限；
- 硬止损、目标价和盈亏比规则；
- 风险预算、现金、单股、总仓位、行业集中度与整手仓位计算；
- 持仓加仓、减仓、退出判断；
- 四个内部计划状态和用户 Decision 映射；
- preview/API 序列化、哈希、保存与执行审计；
- 前端状态和策略展示。

此外，`prepare_indicators` 仍位于 `app/services/technical.py`，Feature 技术模块通过稳定包装调用它；本阶段没有移动更广泛的技术研究页面逻辑。

## 8. Strategy Engine 后续计划

下一阶段可在不改变 API 的前提下：

1. 定义 Strategy、StrategyRule、RuleResult 和 Decision 领域类型；
2. 让规则只接收 `FeatureSnapshot` 与账户风险上下文；
3. 把 12 道 Gate 逐项迁移为纯规则；
4. 建立旧中文 Gate 状态和新规则状态的兼容映射；
5. 保留现有 trade plan 输出适配器，直到 Golden Master 证明行为一致；
6. 最后再处理状态统一、数据库迁移和前端消费方式。

阶段 C 不应被理解为第二套策略或策略优化；它只建立了确定性事实层。
