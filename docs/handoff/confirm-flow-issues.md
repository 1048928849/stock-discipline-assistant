# Confirm Flow 已知问题

## 当前流程

Confirm收到SaveRequest后从请求重建PreviewRequest，重新执行完整Application流程，再比较新旧preview_hash。Hash一致才创建TradePlan和冻结Snapshot。

## 为什么重新计算

- 防止行情、账户权益、可用现金和持仓在预览后变化；
- 防止规则版本或参数变化；
- 防止用户提交过期或篡改的preview_hash。

## PreviewSnapshot边界

阶段H继续使用TradePlanSnapshot作为确认快照：已有preview携带hash，Confirm重新计算并校验后，`save_preview`明确将校验通过的内容冻结为Snapshot，再写入engine_snapshot。

本阶段不改为“直接信任客户端Snapshot”，因为API只提交hash和原请求，没有服务端持久化的待确认Preview实体。取消重算需要新增服务端Snapshot存储、失效策略和并发控制，涉及数据库/API变化。

## 当前风险

- 重复执行Feature、Strategy、Risk、Decision和PricePlanner；
- 可能再次触发规则版本准备副作用；
- 两次分析之间任一输入变化都会要求用户重新预览，这是当前安全行为。

## 后续处理

未来可建立有过期时间的服务端PreviewSnapshot Repository，以snapshot_id+hash确认；在此之前保留重新计算。
