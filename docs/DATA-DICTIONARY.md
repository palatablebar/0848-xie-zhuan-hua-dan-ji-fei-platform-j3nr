# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `call_id` | text | 主键 |
| `subscriber` | text | 业务字段 |
| `start_at` | datetime | 业务字段 |
| `end_at` | datetime | 业务字段 |
| `charged_seconds` | int | 业务字段 |
| `rate_per_min_cents` | int | 业务字段 |
| `direction` | text | 业务字段 |

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。

## 退款对账字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `refund.confirmed_seconds` | int | 已确认可退时长（秒） |
| `refund.pending_seconds` | int | 待补证时长（秒） |
| `refund.total_overcharge_seconds` | int | 可证多收合计（秒） |
| `refund.confirmed_amount_cents` | int | 已确认可退金额（分），按整分钟计 |
| `refund.by_attribution` | list | 按 `号码|方向` 汇总的已确认时长；归属有争议且未补证的记为 `未归属` |
| `refund.pending_reasons` | list | 待补证原因码及计数 |
| `refund.claims[].baseline_seconds` | int | 补证前即可确认的时长 |
| `refund.claims[].ceiling_seconds` | int | 该话单号可证多收上限 |
| 补证入账 `call_id` | text | 目标话单号 |
| 补证入账 `seconds` | int | 本次补登时长，超过待补证余额时按余额截断 |
| 补证入账 `evidence` | text | 证据编号，与批次、话单号一起保证幂等 |
