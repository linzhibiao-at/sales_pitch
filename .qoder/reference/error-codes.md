---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 错误码

## 规则

- `code` 直接使用 **HTTP 状态码数字**，不引入 `模块_编号` 形式的字符串错误码（属有意简化：本项目对外仅一个业务接口，状态码语义已足够）。
- 对外错误统一信封 `{"code": <int>, "message": <str≤500字>, "trace_id": <hex>}`，由 `main.py` 三个异常处理器保证，业务代码不得手工拼装。
- **新增错误场景必须先在本文件登记再写代码**，并在测试中覆盖对应错误分支。

## 鉴权与限流（auth.py / routers/sales_pitch.py）

| code | message（精确串） | 触发条件 | 抛出位置 |
| --- | --- | --- | --- |
| 401 | `API key required` | `auth.enabled=true` 且受保护路径请求无 `X-API-Key` 头 | auth.py `verify_api_key` |
| 401 | `invalid API key` | Key 不在白名单 / `status≠active` / 已过期 | auth.py `ApiKeyStore.get` 后校验 |
| 403 | `access denied: api not allowed` | Key 的 `allowed_apis` 不含 `sales_pitch` | auth.py `verify_api_key` |
| 429 | `rate limit exceeded: {qpm} req/min` | 分钟桶计数超 QPM（硬限制） | auth.py `RateLimiter.check` |
| 429 | `daily limit exceeded: {daily}/day` | 天桶计数超日量（硬限制） | auth.py `RateLimiter.check` |
| 429 | `queue full, try again later` | 排队队列满（`queue_size`） | auth.py `ConcurrencyLimiter.acquire` |
| 429 | `queue timeout after {n}s` | 排队等待超时（`queue_timeout`） | auth.py `ConcurrencyLimiter.acquire` |
| 401 | `invalid app_id` | `allowed_app_ids` 配置存在且 `body.app_id` 不在白名单 | routers/sales_pitch.py |
| 401 | `app_id mismatch with API key` | `body.app_id` ≠ Key 绑定的 `app_id` | routers/sales_pitch.py |
| 400 | `app_id required` | `body.app_id` 清理后为空 | routers/sales_pitch.py |

注：`auth.log_only=true`（灰度过渡）时，缺 Key / 错 Key 仅记日志放行，不返回 401。

## 入参与依赖（main.py / routers / services）

| code | message | 触发条件 | 抛出位置 |
| --- | --- | --- | --- |
| 422 | 字段级错误串（`字段: 原因; ...`） | Pydantic 入参校验失败 | main.py `_validation_exception_handler` |
| 503 | `agent service unavailable (Redis or LLM init failed)` | Agent 栈模块级初始化失败（`_pitch_svc=None`） | routers/sales_pitch.py |
| 503 | `sales pitch generation failed (empty agent output)` | Agent 返回但无非空 AI 消息（含 LLM 内容过滤触发） | service 返回 `{"error": ...}` → router 转 503 |
| 503 | `audit disabled` | `request_audit.enabled=false` 时查审计详情 | routers/audit.py |
| 404 | `not found` | 审计详情按 `trace_id` 未命中 | routers/audit.py |
| 500 | `{异常类型}: {异常信息}` | 未捕获异常全局兜底（含 Agent/LLM 调用抛出的异常，如上游鉴权失败、网络错误） | main.py `_unhandled_exception_handler` |

## 分段语义速查

`401` 鉴权 → `403` 权限 → `422` 入参 → `429` 限流排队 → `500` 未预期异常 → `503` 依赖不可用（服务降级）。

## 调用方处理建议

- 拿到 `trace_id` 后可直接调用 `GET /v1/audit/requests/{trace_id}` 查服务端视角的完整出入参（审计开启时）。
- `429` 按 `Retry-After` 语义延迟重试（当前未回退避 header，建议固定退避 ≥ 1s）。
- `503` 表示服务侧降级（Redis / LLM 初始化失败或空输出），可重试；`500` 不建议自动重试。
