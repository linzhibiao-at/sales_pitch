---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 编码规范总览

## 规范文档

| 文档 | 内容 |
|---|---|
| [naming.md](naming.md) | 命名规范 |
| [dependencies.md](dependencies.md) | 依赖装配（Depends / 构造传参 / 模块单例） |
| [logging.md](logging.md) | 日志（logger 获取 / 脱敏 / 降级日志） |
| [error-handling.md](error-handling.md) | 错误处理与静默降级 |
| [testing.md](testing.md) | 测试（88 基线 / mock 风格） |
| [guardrails.md](guardrails.md) | 机械守护规则（GR-01~GR-08 硬门禁 + 一键检查脚本） |

## 基本原则

- 遵守 Python 3.12 语法与类型注解风格（`from __future__ import annotations` + PEP 604 `str | None`）。
- 遵守 FastAPI + Pydantic v2 生态，不引入其他 Web / ORM 框架。
- 遵守 DeepAgent Harness 装配约束（model 必须是 `BaseChatModel` 子类）。
- 保持模块短小、可测试、可审查；纯函数优先（build_* / parse_* 便于单测）。

## 分层约束

依赖方向固定为：

```text
models / config → infra → llm → agent → services → routers → main
```

任何新增代码都必须放入正确层级，不允许为了方便跨层调用。详见 `../architecture/boundaries.md`。

## 代码规模

- 单个 `.py` 文件（`backend/` 下）不超过 **300 行**。
- 单个函数不超过 **50 行**（不含 docstring 与纯注释行）。
- 超出限制时优先拆分职责，而不是压缩可读性。
- 当前基线：`backend/` 最大文件 auth.py 293 行（贴上限，新增职责时优先考虑拆分）。

## 依赖装配

- 请求级依赖：FastAPI `Depends`。
- 类依赖：构造函数显式传参（可注入 mock），默认 None 时内部创建。
- 进程级单例：模块底部显式声明，禁止散落的隐式全局。
- 详见 `dependencies.md`。

## 日志

- 禁止 `print()`；禁止 `traceback.print_exc()`。
- 统一 `logging.getLogger(__name__)`，懒格式化 `logger.info("x=%s", v)`。
- 详见 `logging.md`。

## 外部调用

- LLM：禁止业务代码直接构造 `ChatOpenAI` / 裸调 OpenAI 协议，统一走 `llm/factory.py`。
- HTTP：如需新增外部 HTTP 调用，统一经 `httpx` 并封装到 infra 层客户端，禁止散落在 services。
- Redis / MySQL：统一经 `infra/` 客户端，必须静默降级。

## 错误处理

- 对外错误统一信封 `{"code", "message", "trace_id"}`（main.py 异常处理器保证）。
- 业务错误用 `HTTPException(status_code, detail)`；完整错误码字典见 `../reference/error-codes.md`。
- 详见 `error-handling.md`。

## 测试

- 新增代码必须补充 pytest 测试；`tests/` 全绿（88 基线不回退）。
- 测试不依赖外部资源（Redis / MySQL / LLM 全 mock）。
- 详见 `testing.md`。

## 提交信息

- `feat:` 新功能 / `fix:` 修复 / `refactor:` 重构 / `docs:` 文档 / `test:` 测试
