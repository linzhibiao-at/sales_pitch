---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 依赖装配规范

装配分三种场景，规则如下。

## 基本规则

1. **请求级依赖**：FastAPI `Depends`（generator 依赖用 `try/finally` 保证释放）。
2. **类依赖**：构造函数显式传参，默认 `None` 时内部创建（便于测试注入 mock）。
3. **进程级单例**：模块底部显式声明（`_xxx` 命名），禁止散落的隐式全局。

## 推荐写法（类依赖 + 测试注入）

```python
class RequestAuditLogger:
    def __init__(self, client: Any = None, enabled: bool | None = None) -> None:
        self._enabled = get_request_audit_enabled() if enabled is None else bool(enabled)
        if client is not None:
            self._client = client          # 测试注入 mock
        elif self._enabled:
            from backend.infra.mysql import MysqlClient
            self._client = MysqlClient()   # 生产默认
        else:
            self._client = None            # 关闭时不建连接
```

延迟 import（函数内 import infra）是有意为之：避免审计关闭时仍建立外部连接。

## 推荐写法（请求级 Depends，generator 依赖）

```python
async def verify_api_key(request: Request):
    acquired = False
    try:
        ...
        acquired = True
        yield
    finally:
        if acquired:
            _concurrency_limiter.release(app_id)   # 并发槽必须释放
```

## 推荐写法（进程级单例）

```python
# auth.py 模块底部：单例集中声明，一眼可见
_api_key_store = ApiKeyStore()
_rate_limiter = RateLimiter()
_concurrency_limiter = ConcurrencyLimiter()
```

## 禁止写法

```python
# ❌ 请求内重复构建 Agent 栈（昂贵对象，必须模块级装配一次）
def v1_generate(...):
    agent = build_agent(...)
    ...

# ❌ 散落全局可变状态
_count = 0
def handler():
    global _count
```

## Agent 栈装配（唯一例外：模块级重型装配）

`routers/sales_pitch.py` 在 **import 时**执行一次 `_init_agent_stack()`：

```text
init_redis() → load_resources() → create_sales_pitch_llm() → build_agent() → SalesPitchService
```

- 失败降级：任何一步异常 → `_pitch_svc = None` → 请求 503，进程存活。
- 4 worker 部署下每个 worker 各装配一份（资源加载是覆盖语义，无冲突）。

## 原因

- 构造函数显式传参让依赖关系在对象创建时完整表达，单测无需 patch 全局。
- Depends generator 的 `finally` 保证并发槽/连接等资源在异常路径也释放。
- 集中声明的单例便于在 guardrails/评审中审计全局状态。

## 校验

分层 import 检查见 `guardrails.md`（routers 不得 import agent/llm/infra）。
