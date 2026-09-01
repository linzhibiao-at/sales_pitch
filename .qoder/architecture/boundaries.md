---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 模块边界和依赖规则

## 依赖方向

代码依赖必须保持单向：

```text
models / config → infra → llm → agent → services → routers → main
```

禁止 routers 直接 import agent / llm / infra；禁止 services import routers；禁止 infra / llm 反向依赖上层。

## models（backend/models.py）

入参的唯一定义处。

允许：

- Pydantic BaseModel + `field_validator` 输入清理（HTML 标签剥除、lone surrogate 剔除、strip）。
- 模块级清理正则常量与纯函数。

禁止：

- import 任何 `backend.*` 内部模块。
- 在 validator 中做业务判断（如查库、调 LLM）。

## config（backend/config.py）

配置的唯一读取处。

允许：

- `load_config()` + mtime 缓存；`get_<section>_<key>()` 系列纯读取函数；环境变量覆盖逻辑。

禁止：

- 承载业务逻辑；返回可变全局状态（返回 dict 应视为只读快照）。

## infra（backend/infra/）

外部资源的可选客户端，**必须静默降级**。

允许：

- `redis.py`：连接 + ping 校验，装配 RedisSaver / RedisStore。
- `mysql.py`：连接 + 启动自动建表 + insert/query/count，连接失败置 `available=False`。
- 内部 `threading.Lock` 串行化单连接。

禁止：

- 业务语义（如"话术失败应重试"）；抛出异常到主链路（失败只能 log + 返回空/None）。

## llm（backend/llm/factory.py）

LLM 的唯一构造处。

允许：

- `_build_chat_openai()` 私有构造（空 api_key 用占位符让构造通过）；`create_sales_pitch_llm()` 返回 primary `ChatOpenAI`（**必须是 BaseChatModel 子类**，fallback 挂 `primary._fallback_model` 属性，DeepAgent 不接受 `RunnableWithFallbacks`）；`create_summarization_llm()` 压缩用便宜模型。

禁止：

- 业务代码直接 `ChatOpenAI(...)`；在 factory 中拼业务 prompt。

## agent（backend/agent/）

DeepAgent Harness 的装配与定制。

允许：

- `loader.py`：`.sales_pitch/` 资源加载到 RedisStore + `build_agent()`。
- `middleware.py`：`AlwaysLoadMemoryMiddleware`（每次 invoke 重载记忆）。

禁止：

- 处理 HTTP 请求/响应对象；直接读 `config.yaml`（经 `backend.config` 函数）。

## services（backend/services/）

业务用例入口。

允许：

- `sales_pitch_service.py`：文本块构建（纯函数）→ `agent.ainvoke()` → 提取 AI 消息 → `finally` 中 `_write_audit()`。
- `request_audit.py`：`build_*_doc` 纯函数构造审计文档；`RequestAuditLogger` 写/查（失败静默，`logger.warning(exc_info=True)`）。
- 编排多个 infra 客户端与 agent。

禁止：

- import routers；输出 HTTP 状态码语义（业务失败返回 `{"error": ...}`，由 routers 决定 503）。

## routers（backend/routers/）

HTTP 协议边界。

允许：

- 参数接收、app_id 白名单与 Key 绑定校验、调用 service、错误转 HTTPException、组装响应 dict。

禁止：

- 写业务规则与 prompt；直接调用 agent / llm / infra；手动处理数据库。

## main（backend/main.py）

app 骨架。

允许：

- 中间件（trace_id / debug 日志）、异常处理器（统一错误信封）、路由挂载、`/health`。

禁止：

- 业务逻辑；直接依赖 agent / llm / infra。

## 装配规则（Python 版"依赖注入"）

- 请求级依赖用 FastAPI `Depends`（如 `auth.verify_api_key`）。
- 类依赖走构造函数显式传参 + 默认 None 内部创建（如 `RequestAuditLogger(client=..., enabled=...)`，测试可注入 mock）。
- 进程级单例在模块底部显式声明（如 auth.py 的 `_api_key_store` / `_rate_limiter`）。
- Agent 栈在 `routers/sales_pitch.py` 模块级装配一次；禁止请求内重复构建。

详见 conventions/dependencies.md。
