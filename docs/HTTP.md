# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `POST /api/runs/{id}/refund`：补证入账，提交 `call_id`、`seconds`、`subscriber`、`direction`、`evidence`、`note`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

`GET /api/runs/{id}` 与 `POST /api/runs` 返回的 `refund` 字段包含每个话单号的
`confirmed_seconds`（已确认可退时长）、`pending_seconds`（待补证时长）、`pending_reasons`
（待补证原因码，如 `VERSION_CONTRADICTS_OVERCHARGE`、`VERSION_DELTA_UNPROVEN`、
`ATTRIBUTION_DISPUTED`），以及汇总的 `by_attribution`。原因码的可读文案见
`GET /api/catalog` 的 `refund_reasons`。

补证入账按 `(批次, 话单号, 证据)` 幂等：同一证据重试返回既有记录（`created: false`），
不重复计入。补证时长以该话单号的可证多收为上限。归属必须落在该话单号的版本范围内，
否则返回 `REFUND_ATTRIBUTION`；已全部确认的话单号返回 `REFUND_NOT_PENDING`。
