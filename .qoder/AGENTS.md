# AGENTS.md

## 项目简介

FILA 营销话术生成服务（`sales_pitch`）：导购提交**顾客画像 + 商品信息**，系统经 DeepAgent Harness 调用 LLM 生成可直接发给顾客的个性化营销话术。基于 Python 3.12 + FastAPI + LangChain/LangGraph + Redis + MySQL。

## 技术栈基线（不允许擅自升级）

- Python: **3.12**（`.venv` 虚拟环境；LangGraph 系依赖不兼容 3.14+，禁止升级 Python 小版本）
- Web: FastAPI ≥ 0.110.0 + uvicorn ≥ 0.27.0（生产 4 worker）
- 数据校验: Pydantic ≥ 2.6.0（入参模型 + 输入清理）
- Agent: deepagents ≥ 0.7.9 + langchain-openai ≥ 0.3.0 + langgraph-checkpoint-redis ≥ 0.1.0
- 存储: Redis ≥ 5.0.0（对话 checkpoint + Agent 资源 store）；MySQL via pymysql ≥ 1.1.0（仅审计落库，自动建表）
- 测试: pytest（基线 **95 个用例全绿**）
- 部署: Docker（python:3.12-slim）+ K8s（prod: `f-aifit` / dev: `mgs-d`）

## 快速导航

| 你想做什么 | 去哪里看 |
|-----------|---------|
| 了解系统架构 | .qoder/architecture/overview.md |
| 了解模块边界和依赖规则 | .qoder/architecture/boundaries.md |
| 了解请求全链路数据流 | .qoder/architecture/data-flow.md |
| 了解编码规范 | .qoder/conventions/README.md |
| 了解机械守护规则（可本地执行） | .qoder/conventions/guardrails.md |
| 了解当前迭代任务 | .qoder/plans/current-sprint.md |
| 了解 API 规范 | .qoder/reference/api-spec.yaml |
| 了解错误码 | .qoder/reference/error-codes.md |
| 了解测试规范 | .qoder/conventions/testing.md |
| 了解历史设计决策 | docs/design-doc/技术架构文档.md |

## 硬性规则（必须遵守）

1. **依赖方向单向**：`models/config → infra → llm → agent → services → routers → main`；routers 禁止直接依赖 agent / llm / infra（必须经 services）
2. 禁止 `print()` / `traceback.print_exc()`，统一 `logging.getLogger(__name__)`
3. 单文件（`backend/` 下 `.py`）≤ 300 行；单函数 ≤ 50 行
4. LLM 调用统一走 `backend/llm/factory.py` 双通道（DashScope 主 + 安踏网关 fallback），业务代码禁止直接构造 `ChatOpenAI`
5. 外部资源（Redis / MySQL）必须**静默降级**：不可用时服务可启动、主链路不崩（话术降级 503 / 审计返空）
6. 入参必须过 Pydantic 清理（HTML 标签剥除 + lone surrogate 剔除），防 LLM 内容过滤与 UnicodeEncodeError
7. 敏感信息（api_key / 密钥 / 图片 base64 / 完整 prompt）禁止入日志，统一走 `redact_for_log()` 脱敏
8. 新增代码必须有 pytest 测试；提交前 `tests/` 全绿（不回退 95 基线）
9. `config/api_keys.yaml` 不入 Git；密钥只经环境变量注入（`DASHSCOPE_API_KEY` / `ANTA_LLM_API_KEY` / `MYSQL_URL` 等）
10. `logging_config.setup_logging()` 必须在所有 `backend.*` 导入之前调用（见 main.py 头部）

## 提交规范

- `feat:` 新功能
- `fix:` 修复
- `refactor:` 重构
- `docs:` 文档
- `test:` 测试
