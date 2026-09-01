---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# Backlog

## 工程基础

- 接入 GitHub Actions：`.github/workflows/guardrails.yml` 复跑 `.qoder/conventions/guardrails.md` 一键脚本（PR 触发）。
- 接入 ruff（lint + format 检查，先 `--select` 少量规则跑通再逐步收紧）。
- 接入 pytest-cov，行覆盖率门槛不低于 80%（先测当前基线再定值）。
- 新增 `pytest.ini`（显式 rootdir / testpaths，避免依赖运行目录）。
- 统一行尾：仓库混用 CRLF/LF，加 `.gitattributes`（`*.py text eol=lf`）。

## 安全（优先）

- `config.yaml → models.sales_pitch_llm.primary.api_key_env` 当前内嵌真实密钥值：恢复为环境变量名 `DASHSCOPE_API_KEY`，并**轮换该密钥**。
- `config/api_keys.yaml` 已被 Git 跟踪（违反 AGENTS.md 规则 9）：`git rm --cached` + 补 `.gitignore`。
- `config.yaml → mysql.url` 含明文口令：改空串，仅经环境变量 `MYSQL_URL` 注入。

## 架构治理

- 拆分 `sales_pitch_service.generate`（60 有效行，guardrails 棘轮白名单）：prompt 文本块组装、Agent 结果提取各成私有函数，拆完删除白名单登记。
- `auth.py` 292 行贴近 300 上限：将 `ApiKeyStore` / `RateLimiter` / `ConcurrencyLimiter` 拆为 `backend/auth/` 包内独立模块。
- 限流为进程内近似（4 worker 下实际限额 ≈ 配置值 × 4）：评估 Redis 全局限流或按 worker 数折算配置。
- `routers/user.py` 的 `GET /v1/users/me` 为占位实现，未接鉴权：接入真实用户体系前保持占位并在 API 文档标注。

## 功能模块

- 会话管理接口：按 `session_id` 删除 / 过期 LangGraph thread（当前 thread 无限增长，仅靠 SummarizationMiddleware 压缩）。
- 审计查询增强：`ts_from`/`ts_to` 范围索引验证（当前仅 `idx_trace_id` / `idx_ts`）。
- 话术风格 / 渠道预设扩展：`_PITCH_STYLE_LABELS` / `_CHANNEL_LABELS` 配置化（移入 `.sales_pitch/` 资源）。
- 前端 `web/`（Vue 3）与后端联调规范（暂不在后端 harness 范围）。

## 文档

- 补充 API 调用示例（curl，含鉴权头与限流响应头）。
- 补充本地启动说明（`.venv` 创建、`setup.sh`、Redis/MySQL 可选依赖）。
- 补充发布与回滚流程（K8s `deployment-prod.yaml` / `deployment-dev.yaml`，prod: `f-aifit` / dev: `mgs-d`）。
- 建立错误码变更流程（新增错误场景先改 `../reference/error-codes.md` 再写代码，已在规范中约定，需评审落地）。
