---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 命名规范

## 模块 / 文件命名

snake_case，按层组织，与目录一一对应：

```text
backend/
  models.py / config.py / auth.py / main.py
  routers/sales_pitch.py
  services/sales_pitch_service.py
  services/request_audit.py
  agent/loader.py / agent/middleware.py
  llm/factory.py
  infra/redis.py / infra/mysql.py
```

## 类命名

PascalCase，按职责加后缀：

- 路由处理：无需类，函数式路由
- 服务类：`XxxService`（`SalesPitchService`）
- 审计/客户端：`XxxLogger` / `XxxClient`（`RequestAuditLogger` / `MysqlClient`）
- 中间件：`XxxMiddleware`（`AlwaysLoadMemoryMiddleware`）
- 限流器：`XxxLimiter`（`RateLimiter` / `ConcurrencyLimiter`）
- Pydantic 入参：`XxxRequest`（`SalesPitchRequest`），子结构 `XxxInfo`（`SalesPitchCustomerInfo`）

## 函数命名

snake_case，动宾结构，表达明确意图。

推荐：

- `build_customer_block` / `build_sales_pitch_doc` / `build_audit_query`
- `insert_audit` / `query_audit` / `count_audit`
- `verify_api_key` / `init_redis` / `load_resources`
- `create_sales_pitch_llm` / `get_by_trace_id`

避免：

- `do_it` / `handle` / `process_data` / `deal`

## 私有成员

- 私有函数 / 方法 / 模块常量：前导下划线（`_build_chat_openai` / `_write_audit` / `_row_to_doc` / `_CREATE_TABLE_SQL`）。
- 前导下划线 + 大写 = 模块私有常量（`_SURROGATE_RE` / `_HTML_TAG_RE`）。

## 配置读取函数

`backend/config.py` 统一为 `get_<section>_<key>`：

- `get_mysql_url` / `get_mysql_table` / `get_request_audit_enabled`
- `get_auth_config` / `get_summarization_config` / `get_agent_resource_dir`

## 路由函数

`api_<资源>_<动作>` 或 `v1_<动作>`（保持现状两种风格，新增路由沿用所在文件已有风格）：

- `v1_sales_pitch_generate`
- `api_audit_requests` / `api_audit_request_detail`

## 数据库命名

- 表名小写下划线：`request_audit`。
- 字段名小写下划线：`trace_id` / `session_id` / `input_json`。
- JSON 列统一 `_json` 后缀（TEXT 存储，读写经 `_to_json` / `_from_json`）。
- 索引 `idx_<列名>`：`idx_trace_id` / `idx_ts`。

## Redis 命名

- Key 前缀：`sp_checkpointer`（短期记忆）/ `sp_store`（Agent 资源）。
- Store namespace 固定 `("fileSystem",)`；文件路径 `/memory/AGENTS.md`、`/soul/{name}`、`/skills/sales-pitch/SKILL.md`。

## 环境变量

UPPER_SNAKE：`DASHSCOPE_API_KEY` / `ANTA_LLM_API_KEY` / `REDIS_HOST` / `REDIS_PORT` / `MYSQL_URL` / `FILA_AGENT_DEBUG_API_IO`。

## 测试命名

- 文件：`test_<被测模块>.py`（`test_sales_pitch.py` / `test_auth.py` / `test_request_audit.py`）。
- 类：`XxxTest`（`SalesPitchServiceGenerateTest`）。
- 方法表达场景和期望：`test_ok_result_and_audit` / `test_disabled_skips_write` / `test_size_clamped`。
