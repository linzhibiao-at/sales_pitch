---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 错误处理规范

## 基本原则

- 对外错误统一信封：`{"code": <HTTP状态码>, "message": <str≤500字>, "trace_id": <hex>}`。
- 业务/协议错误用 `HTTPException(status_code, detail)`，由 main.py 全局异常处理器统一转信封。
- 禁止向客户端暴露堆栈、SQL、内部路径（全局兜底处理器已保证，业务代码不得绕过）。
- 日志记异常详情（`exc_info=True`），响应只给可理解信息。

## 错误码

完整字典见 `../reference/error-codes.md`（code 直接使用 HTTP 状态码数字，属有意简化——新增错误场景先登记该文档再写代码）。

分段语义：

- `401`：鉴权（缺 Key / Key 无效 / app_id 白名单外 / Key 绑定不匹配）
- `403`：Key 无该接口权限
- `422`：Pydantic 入参校验失败（字段级错误串）
- `429`：限流与排队（QPM / 日量 / 队列满 / 排队超时）
- `503`：依赖不可用（Agent 栈初始化失败 / Agent 空输出 / 审计关闭查详情）
- `500`：全局兜底未捕获异常

## 异常分类（按处理位置）

| 异常 | 处理位置 | 行为 |
|---|---|---|
| `HTTPException`（业务主动抛） | main.py `_http_exception_handler` | 转信封，≥500 记 ERROR，<500 记 DEBUG |
| `RequestValidationError` | main.py `_validation_exception_handler` | 422 + 压缩字段错误串 |
| 未捕获 `Exception` | main.py `_unhandled_exception_handler` | 500 + trace_id，堆栈只进日志 |
| infra 客户端内部失败 | infra / request_audit 内部 | **静默降级**：log + 返回空/None，不上抛 |
| Agent 空输出 | sales_pitch_service | 返回 `{"error": ...}`，由 router 转 503，同时落审计 |
| Agent / LLM 调用异常 | sales_pitch_service | `finally` 落审计后 re-raise → main 兜底 500 |

## 静默降级（本项目特有约定）

外部资源失败**不允许**炸掉主链路：

- MySQL 审计写/查失败 → `logger.warning(..., exc_info=True)` + 返回 None/空列表。
- 审计关闭（`request_audit.enabled=false` 或 `mysql.url` 为空）→ 列表返回 `{enabled: false, total: 0, items: []}`（200），详情 503。
- Redis 初始化失败 → 启动时捕获 → `_pitch_svc = None` → 话术请求 503（服务仍可启动，`/health` 正常，审计路由可用）。
- LLM 空 api_key → 构造用占位符通过（启动不崩），实际调用时报鉴权错误。

## 禁止事项

- 禁止 `traceback.print_exc()` / `print()` 输出错误。
- 禁止吞掉异常后返回成功语义（审计链路的静默降级是**唯一例外**，且必须 `logger.warning(exc_info=True)` 留痕）。
- 禁止把异常 message 原样拼进 200 响应。
- 禁止在业务代码手工拼非标准错误响应体（统一走信封）。
- 禁止 `except Exception: pass`（无日志裸吞）。
