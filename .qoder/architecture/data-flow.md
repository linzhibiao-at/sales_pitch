---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 数据流说明

## 话术生成链路（POST /v1/sales-pitch/generate）

```text
Client
  -> main.py trace_id_middleware        （生成 trace_id → request.state，回写 X-Trace-Id）
  -> main.py debug_api_request_middleware（FILA_AGENT_DEBUG_API_IO 开启时记录入参，脱敏）
  -> auth.verify_api_key                （Key 校验 → allowed_apis → QPM/日量 → 并发排队）
  -> routers/sales_pitch.v1_sales_pitch_generate
       ├── app_id 白名单校验（非白名单 → 401）
       ├── API Key 绑定校验（app_id 与 Key 绑定不一致 → 401）
       └── SalesPitchService.generate()
             ├── build_customer_block / build_products_block / build_requirements_block
             │     （Pydantic 已清理的入参 → 中文 prompt 文本块）
             ├── agent.ainvoke({"messages": [...]}, config={"thread_id": session_id})
             │     ├── AlwaysLoadMemoryMiddleware：每次从 RedisStore 加载 AGENTS.md
             │     ├── SummarizationMiddleware：token > 50000 自动摘要压缩
             │     ├── LLM 双通道：DashScope 主 → 安踏网关 fallback
             │     └── FilesystemPermission：deny 写 /skills/** /memory/** /soul/**
             ├── 反向遍历 messages，提取最后一条非空 AI 消息作为话术
             └── finally: _write_audit() → 入队（后台线程批量写 MySQL，静默降级）
  -> 响应 {session_id, pitch, pitch_style, model, trace_id}
       + X-Trace-Id / X-Queue-Status / X-Queue-Wait / X-Queue-Position 头
```

## 会话续轮（多轮对话）

- 首次请求：调用方不传 `session_id` → 服务生成并随响应返回；前端持久化。
- 后续请求：携带同一 `session_id` → LangGraph 恢复该 thread 的对话历史 → Agent 在上下文中迭代话术。
- 历史超过 5 万 token 时由 SummarizationMiddleware 压缩，保留最近 10 条。

## 审计写入（services/request_audit.py + services/audit_worker.py）

- 触发点：`SalesPitchService.generate()` 的 `finally` 块——成功与失败路径都落审计。
- 异步批量：`write()` 仅入内存队列（微秒级，不阻塞事件循环）；后台 daemon 线程 drain 队列攒批（≤50 条/批），经 `insert_audit_many`（executemany）批量落库。
- 文档构造：`build_sales_pitch_doc(input_block, result, meta)` 纯函数；话术正文只落前 600 字 + 总长度（`pitch_len`），避免文档膨胀。
- 断连自愈：`MysqlClient._ensure_conn` ping 保活 / 重连（含重试一次），MySQL 恢复后审计自动续写。
- 失败静默：队列满丢弃新文档并计数；批量写失败丢弃该批；均只 warning，不影响主链路（宁丢不阻塞）。
- 退出兑底：atexit 时尽力 drain 剩余队列；`stats()` 可观测 queued/written/dropped/failed_batches。

## 审计查询（GET /v1/audit/requests）

```text
Client -> routers/audit -> RequestAuditLogger -> MysqlClient -> request_audit 表
```

- 列表：参数化 SQL（防注入），`ORDER BY ts DESC` + `page`/`offset` 分页（page 优先），返回精简行（话术只含前 80 字）。
- 详情：按 `trace_id` 精确查询，返回完整文档（含完整 `input` / `result` JSON 块）。
- 降级：审计关闭或 MySQL 不可用 → 列表返回 `{enabled: false, total: 0, items: []}`（200）；详情 503。

## 限流排队（auth.py，进程内近似）

- QPM / 日量：分钟桶 + 天桶计数，per app_id，超限 429。
- 并发：`asyncio.Semaphore` 并发槽 + 排队队列；队列满 429、排队超时 429。
- 注意：生产 4 worker 下为**单进程近似**（每 worker 独立计数），实际限额约为配置值 × worker 数。

## 日志和追踪

- 请求入口、鉴权拒绝、限流触发、Agent 初始化失败、外部调用失败、审计失败均需结构化日志。
- 日志统一携带 `[模块]` 前缀（`[auth]` / `[llm]` / `[infra]` / `[router]`）。
- 禁止输出明文 api_key、密钥、图片 base64；prompt 超过 2000 字截断（`redact_for_log()`）。
