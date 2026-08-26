# ES 请求审计落库 + 审计展示页 设计

- 日期: 2026-07-28
- 状态: Draft → 待评审

## 1. 目标与背景

新增「把每次对外请求的输入（图文）、意图解析结果、召回结果、最终搭配推荐结果落库到 ES」的能力，便于后续调优与审计；并新增一个前端只读页面展示这些记录。

现状：
- 对外推荐主链路 `/v1/outfit/recommend` → `RecommendService.external_recommend` → `chat_stream`，`chat_stream` 已按阶段产出 SSE 事件：`session_id`、`intent`、`anchor_skus`、`recall_progress`、`recall_done`、`ranking_reason_done`、`outfit_results`。
- 对外重新生成理由 `/v1/outfit/regenerate-reason` → `RecommendService.external_regenerate`，返回 `{outfit_id, reason}`（缓存命中或 ES 兜底重建）。
- ES 已是项目依赖（`elasticsearch` 7.x），`backend/retrieval/es_client.py:EsClient` 提供通用单文档方法 `index_doc(index_key, doc, doc_id=None)`（不可用/失败返回 None，吞异常）、`search_docs(index_key, body)`。`eval/es_review_store.py` 已有「写 ES 索引」的成熟范式。
- 现有日志：`JsonlLogger` 落盘 `data/logs/online/recommend_YYYYMMDD.jsonl`；`dump_replay` 落盘 per-trace JSON。均非 ES、不可便捷检索/聚合。
- `config.yaml` 的 `elasticsearch.indices` 现有 `skus / outfits / reviews`，索引名遵循 `umalog-q-maiamgs-index-fila-*` 前缀约定。

## 2. 范围

覆盖两条对外 v1 接口，用文档字段 `request_kind` 区分：
- `recommend`：`/v1/outfit/recommend`（含完整 input/intent/recall/result）
- `regenerate_reason`：`/v1/outfit/regenerate-reason`（intent/recall 置空，仅 input + result）

内部 `/chat`、`/recommend/skus`、`/recommend/outfits` 为调试台流量，默认不落库。

## 3. 关键设计决策

1. **单点采集（recommend 路径）**：在 `external_recommend` 遍历 `chat_stream` 时顺手采集 `intent / anchor_skus / recall_done / ranking_reason_done / outfit_results` 事件载荷（均为已有 SSE 字段，零额外计算），循环结束、reshape 出参后拼一条审计文档写入。`chat_stream`（内部 `/chat` 也复用）完全不动，审计逻辑集中一处、可独立测试。
2. **一请求一文档**：单条 ES 文档嵌套 `input / intent / recall / result`，附顶层 `trace_id / session_id / app_id / caller / request_kind / ts / elapsed_ms / status`。
3. **图片不入库 base64**：外部入参为 `image_url`（URL），只存 `image_url` + 抓取字节的 `image_sha1`（去重/追溯）。不把多 MB base64 塞进 ES；原图可凭 URL 取回。
4. **写入不阻塞、不连坐**：ES 不可用或写失败只 `logger.warning`，绝不影响用户请求结果与延迟。recommend 路径用 `await asyncio.to_thread(audit.write, doc)`；regenerate 路径（同步端点）直接同步 `audit.write(doc)`，均 try/except 吞掉。
5. **`refresh=False` 写入**：审计走近实时即可，`index_doc` 增加 `refresh: bool = True` 形参，审计传 `False`，省去每文档强制 refresh 开销，降低延迟。
6. **配置开关**：`elasticsearch.request_audit.enabled`（默认 `true`）控制落库；查询 API 在禁用或 ES 不可用时返回空列表 / 503。

## 4. 数据模型（ES 文档）

索引 `umalog-q-maiamgs-index-fila-requests`（key=`requests`）。文档 `_id` 由 ES 自动生成（便于同 trace 多次重试各自留痕）。

```jsonc
{
  "trace_id": "hex",            // 请求级 trace_id（与响应头 X-Trace-Id 一致）
  "session_id": "hex|string",
  "app_id": "string",           // 入参 app_id
  "caller": "string|null",      // API Key 绑定的 app_id（auth.enabled 时）
  "request_kind": "recommend|regenerate_reason",
  "ts": "2026-07-28T...+08:00", // UTC iso，请求结束时刻
  "elapsed_ms": 1234,           // external_recommend / external_regenerate 全程耗时
  "status": "ok|error",
  "error": "string|null",       // status=error 时的简要信息（不泄堆栈）
  "input": {
    "input_sku_id": "string",
    "image_url": "string|null",
    "image_sha1": "string|null", // 抓取字节 sha1；抓取失败则 null
    "message": "string|null",
    "tryon": false,
    "reason_style": "string|null"
  },
  "intent": {                   // regenerate_reason 路径为 null
    "intent": { /* UserIntent.model_dump() */ },
    "method": "string",
    "confidence": 0.0,
    "llm_fallback": false,
    "image_override": false,
    "anchor_source": "string|null",
    "image_role": "string|null"
    // 来自 IntentResult.to_sse_fields()
  },
  "recall": {                   // regenerate_reason 路径为 null
    "anchor_sku_id": "string|null",
    "mode": "string|null",
    "recalled_sku_count": 0,
    "composed_outfit_count": 0,
    "before_dedupe": 0,
    "after_dedupe": 0,
    "paths": { "image_vector": {"count":0,"elapsed_ms":0}, "text_vector":{...}, "query2es":{...}, "complementary_model":{...} },
    "roles": {},
    "deduped_outfit_ids": ["..."]
  },
  "result": {
    // recommend: { "outfits": [ {outfit_id, outfit_rank, reason, items:[{sku_id,role,title,spu_id,id_goods}]} ] }
    // regenerate_reason: { "outfit_id": "...", "reason": "..." }
  }
}
```

字段裁剪：`result.outfits[].items` 不含图片 URL（避免大字符串与重复图）；详情页需要图时按 sku_id 走 `/skus/{sku_id}` 取。`message` 原样保留（已过模型层 surrogate/HTML 清洗）。`recall.paths` 来自 `recall_done` / `recall_progress` 事件字段。

## 5. 组件与文件清单

### 新增

- `backend/services/request_audit.py` — `RequestAuditLogger`
  - `__init__(self, es: EsClient | None = None)`：持有 `EsClient`（复用单例或自建），读 `elasticsearch.request_audit.enabled`。
  - `enabled` 属性：配置开关与 ES 可用性同时为真。
  - `write(self, doc: dict) -> None`：同步；`if not enabled: return`；`self._es.index_doc("requests", doc, refresh=False)`；try/except 吞异常只 `logger.warning`。
  - `build_recommend_doc(...)` / `build_regenerate_doc(...)`：纯函数式构造文档，便于单测（不依赖 ES）。
- `web/audit.html` + `web/audit.js` — 审计展示页（只读列表 + 详情）。

### 改动

- `backend/retrieval/es_client.py` — `index_doc` 增 `refresh: bool = True` 形参，传给 `self._client.index(...)`（`refresh=refresh`）；并在头部加防御：`index_key not in self._indices` 时直接返回 None（避免 KeyError 逃逸到 try 之外，支持 `requests` 未配置时静默降级）。其余签名/行为不变。
- `backend/config.py` — `get_elasticsearch_indices` 的 `keys` 元组追加 `"requests"`；并在解析处允许 `requests` 缺省（与 `reviews` 同策略：缺省则不加入返回 dict，调用方按需校验）。新增 `get_request_audit_enabled(cfg=None) -> bool` 读 `elasticsearch.request_audit.enabled`。
- `backend/services/external_recommend.py`
  - `external_recommend`：遍历 `chat_stream` 时除 `outfit_results` 外采集 `intent / anchor_skus / recall_done / ranking_reason_done`；reshape 后用 `build_recommend_doc` 拼文档，`await asyncio.to_thread(self._audit.write, doc)`（try/except）。
  - `external_regenerate(self, req, *, trace_id, app_id, caller)`：增加 `trace_id / app_id / caller` 形参；构造 `build_regenerate_doc` 文档，`self._audit.write(doc)`（同步，try/except）。
- `backend/main.py`
  - `v1_outfit_regenerate`：把 `_request_trace_id(request)` 与 `caller` 传入 `external_regenerate`（与 recommend 路径对齐）。
  - 新增 `GET /api/audit/requests`：先校验 `get_request_audit_enabled()` 与 `EsClient.available`，禁用/不可用直接返空列表；否则查询参数 `trace_id / app_id / session_id / request_kind / status / ts_from / ts_to / size / offset`，`search_docs("requests", body)` 构 bool 过滤 + `ts` 倒序 + 分页，返回精简行。
  - 新增 `GET /api/audit/requests/{trace_id}`：同上前置校验，不可用 503；按 `trace_id` term 查首条，返回完整文档，未命中 404。
  - `web/audit.html` 通过现有 `/web` 静态挂载即可访问（`/web/audit.html`）；在 `web/index.html` header 导航加入口（非 presentation 模式显示）。
- `config.yaml`
  - `elasticsearch.indices.requests: umalog-q-maiamgs-index-fila-requests`
  - 新增 `elasticsearch.request_audit.enabled: true`（带注释）

### 测试

- `tests/test_request_audit.py`
  - `build_recommend_doc`：给定 mock 事件载荷，断言文档字段齐全、`result.outfits[].items` 无图片字段、`image_sha1` 正确。
  - `build_regenerate_doc`：断言 `request_kind`、`intent/recall` 为 null、`result` 形状。
  - `RequestAuditLogger.write`：`enabled=False` 不调 ES；ES 抛异常时静默告警不 raise。
  - `external_recommend` 集成（mock chat_stream + mock audit）：主流程返回正常、审计失败仅告警、采集到的事件正确入文档。
  - `index_doc(refresh=False)`：断言透传 refresh 参数（mock client）。

## 6. 错误处理

- ES 不可用 / 索引缺失 / 超时：`index_doc` 已吞异常返 None；审计层再包 try/except，绝不 raise 到业务。
- 鉴权 401（app_id 非法 / mismatch）：请求未进入 service，不落库。
- `chat_stream` 内部异常：由现有全局 `Exception` handler 返回 500 + trace_id；审计文档不写（请求未走完）。可接受——审计面向「成功 + 业务降级」路径；纯 500 由服务端日志覆盖。
- 查询 API：ES 不可用或审计禁用 → 列表返空、详情返 503（与 `EsReviewStore` 一致）。

## 7. 部署与回滚

- 索引需预先在 ES 创建（`umalog-q-maiamgs-index-fila-requests`，与现有 `*-fila-reviews` 同集群同前缀）。若未创建，`index_doc` 失败仅告警，不影响线上。
- 回滚：`elasticsearch.request_audit.enabled: false` 即停落库；查询 API 自动返空。无 schema 迁移风险（新索引）。

## 8. 非目标（YAGNI）

- 不做实时流式审计（请求结束一次性写）。
- 不做内部 `/chat`、`/recommend/*` 调试台流量落库。
- 审计页不做编辑/导出/告警，只读列表 + 详情。
- 不存 base64 图片原文。
