---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 测试规范

## 基本要求

- 新增代码必须有对应 pytest 测试，与实现同批提交。
- 提交前 `tests/` 全绿，**94 用例基线只增不减**（guardrails GR-02 机械检查）。
- 测试**不依赖任何外部资源**（Redis / MySQL / LLM / 网络），离线可跑。

运行命令（仓库根目录）：

```bash
.venv/bin/python -m pytest tests/ -q
```

## 测试风格（现有基线）

沿用 `unittest.TestCase` 类组织 + pytest 运行器，不强制迁移 pytest 函数式写法；新增测试跟随所在文件的既有风格。

- 文件：`tests/test_<被测模块>.py`，与 `backend/` 模块一一对应。
- 类：`XxxTest`，按被测职责分组（`SalesPitchRequestValidationTest`）。
- 方法：`test_<场景>_<期望>`（`test_ok_result_and_audit` / `test_disabled_skips_write` / `test_size_clamped`）。

## 测试类型与现有对应

| 类型 | 适用 | 现有文件 |
|---|---|---|
| 模型校验 | Pydantic 必填/长度/清理逻辑（HTML 剥除、surrogate 剔除） | test_sales_pitch.py |
| 纯函数 | `build_*` / `slim_audit_row` / `build_audit_query` 等 | test_sales_pitch.py、test_request_audit.py |
| 服务层 | `SalesPitchService.generate` 编排（成功/空输出/异常/审计） | test_sales_pitch.py |
| 鉴权与限流 | Key 校验、QPM/日量超限、并发排队、401/403/429 分支 | test_auth.py |
| 审计读写 | 文档构造、静默降级、分页与过滤 | test_request_audit.py |

## Mock 约定

**手写 Fake/Stub 类优先**（现状基线，可读、可断言调用次数），不强制 `unittest.mock.patch`：

```python
class _MockAgent:
    """模拟 DeepAgent CompiledGraph，支持 async ainvoke()。"""
    async def ainvoke(self, input_dict, *, config=None): ...

class _FakeAudit:
    def __init__(self):
        self.docs: list[dict] = []
        self.enabled = True
```

- 服务注入用 `SalesPitchService.__new__` + 直接赋值 `_agent` / `_audit`（绕过构造副作用）。
- 审计用 `_FakeAudit` 收集文档断言内容；关闭态用 `enabled=False`。
- 需要热加载配置的测试用 `ApiKeyStore.reload()` / 限流器 `reset()`（auth.py 提供的测试钩子，勿删）。

## 覆盖重点

每个新功能至少覆盖：

- 正常流程（含审计落库断言）。
- 入参非法（`assertRaises(ValidationError)`、422 分支）。
- 输入清理边界：HTML 标签、lone surrogate、空 title、超长截断。
- 外部失败：Agent 抛异常 / 空输出、审计写失败不影响主链路（静默降级分支**必须**有测试）。
- 限流边界：分钟桶/天桶临界值、队列满、排队超时。

## 断言要求

- 必须有明确断言；禁止只调用不断言。
- 错误分支用 `assertRaises` 上下文，必要时断言异常 message。
- 降级分支断言「返回空值 + 不抛异常 + （可选）日志留痕」三要素。

## 测试数据

- 数据在测试内或 `_base(**overrides)` 工厂方法中构造，禁止依赖本地库脏数据。
- 中文/emoji 字符串属正常输入（顾客画像天然含中文），lone surrogate 用 `\ud800` 字面量构造负例。
