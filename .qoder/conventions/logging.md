---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 日志规范

## 基本要求

统一 `logging` 标准库。禁止 `print()`，禁止 `traceback.print_exc()`（guardrails GR-03 机械检查）。

## Logger 获取

模块级一行，logger 名即模块路径（`backend.services.sales_pitch_service` 等）：

```python
logger = logging.getLogger(__name__)
```

- 不新建自定义 logger 名；唯一例外：`api_debug.py` 使用专用命名空间 `fila_agent.api_io`（挂独立 stderr handler，保证 uvicorn 下可见）。
- 不在函数体内重复 getLogger。

## 初始化时序（硬性）

`backend/logging_config.py::setup_logging()` 必须在**所有其他 `backend.*` 导入之前**调用（见 `main.py` 头部注释）。原因：formatter 必须先挂载，否则后续模块的 logger 拿到的是 root 默认格式。

- `setup_logging()` 幂等（`_LOGGING_INITIALIZED` 标记，`force=True` 可重挂）。
- 只接管 `backend` 与 `fila_agent` 两个命名空间，`propagate=False`；uvicorn 自身日志不受影响。
- 级别默认从 `config.yaml → logging.level` 读取，缺省 INFO。
- `httpx` / `openai` / `langchain` / `langgraph` / `deepagents` / `redis` 等第三方库统一降为 WARNING（降噪，勿删）。

## 日志级别

- `debug`：排查信息（422 校验失败明细、<500 的 HTTPException 等）。
- `info`：关键业务节点（生成开始、鉴权 log_only 放行、`log_flow` 出入参）。
- `warning`：**可恢复**的外部依赖失败——静默降级链路必须用它留痕（审计写/查失败、MySQL 不可用）。
- `error`：不可恢复错误（Agent 初始化失败、空输出、≥500 HTTPException、全局兜底异常）。

## 格式约定

- 懒格式化，禁止 f-string 拼日志（`logger.info("x=%s", v)` 而非 `logger.info(f"x={v}")`）。
- 消息前缀 `[模块]` 标识来源：`[auth]` / `[llm]` / `[infra]` / `[router]` / `[营销话术]`。
- 键值用 `key=value` 平铺，便于 grep；不输出整行未截断 JSON。

推荐：

```python
logger.info(
    "[营销话术] 生成开始 trace_id=%s app_id=%s product_count=%d has_customer=%s",
    trace_id, app_id, len(req.products), bool(customer_block),
)
```

## 记录内容

日志必须携带可定位问题的上下文：

- 请求链路：`trace_id`（必有）、`app_id`、`session_id`、`path`。
- Agent 链路：`thread_id`（= session_id）、模型名、`elapsed_ms`。
- 外部依赖：失败对象（Redis / MySQL / LLM 通道）+ 具体原因。

## 异常日志

异常必须作为 `exc_info` 传入，堆栈只进日志、不进响应：

```python
logger.warning("request audit write failed", exc_info=True)
logger.error("[router] Agent 初始化失败（服务降级）: %s", e, exc_info=True)
```

禁止只记 `str(e)` 丢堆栈（审计链路降级除外也无此豁免——必须 `exc_info=True`）。

## 敏感信息（硬性）

禁止直接输出：api_key、密钥、图片 base64、完整 prompt、完整话术正文。统一经 `backend/api_debug.py`：

| 工具 | 用途 |
|---|---|
| `redact_for_log(obj)` | 递归脱敏：`api_key` → `<redacted>`；字符串超 `logging.redact.prompt_max_chars`（默认 2000）截断；列表超 50 截断 |
| `log_flow(tag, payload)` | 结构化出入参 trace（`http_in` / `http_out` / `llm_in` / `llm_out`），仅在调试开关开启时输出 |
| `summarize_http_response(path, data)` | 出参摘要：只记 `keys` + `pitch_len` + 前 200 字预览 |
| `summarize_messages_for_llm(msgs)` | LLM 消息摘要：单条 content 截 400 字 |

调试开关：环境变量 `FILA_AGENT_DEBUG_API_IO`（优先）或 `config.yaml → logging.debug_api_io`。生产默认关闭。

## 禁止事项

- `print()` / `traceback.print_exc()`。
- f-string / `%` 即时拼接（用懒格式化占位符）。
- 业务代码绕过 `redact_for_log()` 直接 `logger.info(payload)`。
- `except Exception: pass` 不留痕（静默降级也必须 `warning + exc_info`）。
- 在 `logging_config.py` / `api_debug.py` 之外自行配置 handler。
