# 评审数据 ES 存储与可配置切换 设计

日期: 2026-07-23
分支: feature/brand_series

## 背景与目标

批量评测页(`/eval/review.html` → `/eval/review_detail.html`)的评审意见当前只存本地
SQLite `eval/reviews.db`。本地单机存储有丢失风险(磁盘故障/容器重建/误删)。

目标:
1. 新建一个 Elasticsearch 索引存放评审数据。
2. 通过配置在 SQLite 与 ES 之间**硬切换**(单后端)。
3. 不迁移历史数据(ES 从零开始)。

## 非目标

- 不做双写、不做 ES→SQLite 自动兜底回退(硬切换语义)。
- 不迁移现有 SQLite 评审。
- 不新增前端导出按钮(本设计仅做存储后端可切换)。
- 不为评论字段做全文检索(当前无此需求)。

## 现状

- `eval/review_store.py`:纯 SQLite,函数 `add_review/get_reviews/delete_review`,
  表 `outfit_reviews`(含 `_migrate`/`_ensure_columns`),`_DB_PATH = eval/reviews.db`。
- `backend/main.py:68,586-610`:`from eval.review_store import add_review, delete_review,
  get_reviews`;三个端点 `GET/POST/DELETE /eval/api/reviews`,`ReviewBody` 含
  data_file/input_sku_id/outfit_id/rating/comment/reviewer/reviewer_role/reviewer_name;
  `DELETE` 形参 `id: int`。
- 前端 `eval/review_detail.js:204,200` 用 `String(r.id)===String(reviewId)`、
  `encodeURIComponent(reviewId)`,故 `id` 为字符串(ES `_id`)无需改前端。
- `backend/retrieval/es_client.py`:`EsClient` 类,`self._indices` 来自
  `get_elasticsearch_indices(cfg)`(必填键 `skus/outfits`),已有 `bulk_upsert_docs`、
  `delete_docs_by_query`、`search`、`get_doc` 等;无单文档 `index`/`delete`。
- `backend/config.py:get_elasticsearch_indices` 必填键 `("skus","outfits")`,缺则抛错。
- `config.yaml elasticsearch.indices`:仅 `skus`/`outfits`。
- ES 集群:umalog 7.9.3,索引须用 `umalog-q-maiamgs-index-*` 前缀。

## 架构

```
backend/main.py
   └─ get_review_store()  ← 工厂,按 config.review.storage 返回单例
         ├─ SqliteReviewStore  (eval/review_store.py 现有逻辑重构为类)
         └─ EsReviewStore      (eval/es_review_store.py 新建)
               └─ 复用 backend.retrieval.es_client.EsClient
```

### 组件

- **`ReviewStore` 协议**(`eval/review_store.py`):
  - `add(data_file, input_sku_id, outfit_id, rating=None, comment=None, reviewer=None,
     reviewer_role=None, reviewer_name=None) -> dict`
  - `get(data_file: str) -> list[dict]`
  - `delete(id: str) -> bool`
- **`SqliteReviewStore`**:现有 `add_review/get_reviews/delete_review` 函数体收进类,
  行为不变(含 `_migrate`/`_ensure_columns`/WAL);保留 `upsert_review` 旧别名(调 `add`)。
- **`EsReviewStore`**:持有一个 `EsClient` 实例,用其 `index_doc/delete_doc/search` 实现。
- **工厂 `get_review_store(cfg=None) -> ReviewStore`**:
  - `cfg` 为空时 `load_config()`;读 `review.storage`,`sqlite`(默认/未知值→warn 后回 sqlite)
    → `SqliteReviewStore`;`es` → `EsReviewStore`。
  - 模块级单例缓存(同进程复用)。

## 配置

`config.yaml` 新增:
```yaml
review:
  storage: sqlite   # sqlite | es  (硬切换,单后端)

elasticsearch:
  indices:
    reviews: "umalog-q-maiamgs-index-fila-reviews"   # 新增,沿用 umalog 前缀
```

`backend/config.py`:
- `get_elasticsearch_indices`:必填键仍 `("skus","outfits")`;**额外把 `reviews` 作为可选键**
  并入返回 dict(旧配置无 `reviews` 不报错)。实现:在必填校验后,
  `if idx.get("reviews"): out["reviews"]=idx["reviews"]`。

## ES 索引与 mapping

文档 `_id`:ES 自动生成,作为返回给前端的 `id`(避免自增 id 在 ES 下的并发竞争)。

mapping(ES 7.9.3,建索引脚本 `scripts/build_fila_reviews_es_index.py`,幂等:存在则跳过):
```json
{ "mappings": { "properties": {
  "data_file":      {"type":"keyword"},
  "input_sku_id":   {"type":"keyword"},
  "outfit_id":      {"type":"keyword"},
  "rating":         {"type":"integer"},
  "comment":        {"type":"text"},
  "reviewer":       {"type":"keyword"},
  "reviewer_role":  {"type":"keyword"},
  "reviewer_name":  {"type":"keyword"},
  "created_at":     {"type":"date","format":"strict_date_optional_time||iso8601"},
  "updated_at":     {"type":"date","format":"strict_date_optional_time||iso8601"}
}}}
```
`created_at/updated_at` 仍由 store 写入 UTC ISO(与 SQLite 一致),
`strict_date_optional_time` 接受带偏移的 ISO8601。

## EsClient 通用方法(新增,非评审专属)

`backend/retrieval/es_client.py` 新增三个通用方法(与 `bulk_upsert_docs`/`delete_docs_by_query`
同级):
- `index_doc(index_key, doc, doc_id=None) -> str | None`:单文档 index,返回 ES `_id`
  (失败返回 None,异常 log)。
- `delete_doc(index_key, doc_id) -> bool`:按 `_id` 删单文档(未命中返回 False,异常 log)。
- `search_docs(index_key, body) -> list[dict]`:发出 search,返回 `[(doc_id, _source), ...]`
  或 `_source` 列表(评审专属 query body 由 `EsReviewStore` 构造,EsClient 不含评审语义)。

均经 `self._indices[index_key]` 解析索引名,`available` 为 False 时安全返回空。

## 数据流

- **add**:`EsReviewStore.add` 组装 doc(写入 `created_at=updated_at=now` UTC ISO),
  调 `es.index_doc("reviews", doc)`(doc_id=None 自动生成),取返回 `_id`,回填 `id` 返回 dict。
- **get**:构造 body `{size:10000, query:{term:{data_file:...}},
  sort:[{created_at:{order:"desc"}}]}`,经 `es.search_docs("reviews", body)` 发出;
  返回 `_source` 列表,每条回填 `id=_id`。
- **delete**:`es.delete_doc("reviews", id)`。

`get` 的 `size=10000` 为单批次评审量上限;超出在 log 记录截断(后续可改 scroll,本设计不做)。

## 错误处理(硬切换)

- `storage=es` 但 ES 不可用(`EsClient.available=False`):`EsReviewStore` 方法抛 `RuntimeError`
  (并在初始化时记一次 warning)。
- `backend/main.py` 三个评审端点用 try/except 包裹 store 调用,捕获 `RuntimeError` →
  `HTTPException(503, detail="评审存储不可用(ES)")`;不静默回退 SQLite;不影响其他端点。

## 文件清单

改动:
- `eval/review_store.py`:`ReviewStore` 协议 + `SqliteReviewStore`(重构)+ `get_review_store`
  工厂 + 保留 `upsert_review` 别名。
- `eval/es_review_store.py`(新):`EsReviewStore`。
- `backend/retrieval/es_client.py`:`+index_doc / +delete_doc / +search_docs`。
- `backend/config.py`:`get_elasticsearch_indices` 并入可选 `reviews` 键。
- `backend/main.py`:`get_review_store` 工厂接入;三端点改调 store;`ReviewBody`/`DELETE`
  形参 `id: int → str`;try/except→503。
- `config.yaml`:`review.storage` 块 + `elasticsearch.indices.reviews`。
- `scripts/build_fila_reviews_es_index.py`(新):幂等建索引。
- `tests/test_review_store.py`(新)。

## 测试

`tests/test_review_store.py`:
- `SqliteReviewStore`:add→get→delete 往返;多人多次评审(同 outfit 多条);字段齐全。
- `EsReviewStore`:假 ES client(Fake,记录调用并模拟 `index` 返回 `_id`、`search` 返回 hits、
  `delete` 返回 found)验证:add 产出 `id`;get 按 `data_file` 过滤且按 `created_at` 倒序;
  delete 命中→True/未命中→False。
- 工厂:`storage=sqlite`→Sqlite、`storage=es`→Es、未知值→默认 sqlite 并 warn。

## 部署与上线

- 在测试集群手动跑一次 `python -m scripts.build_fila_reviews_es_index` 建索引(幂等)。
- `config.yaml` 默认 `storage: sqlite`,先合并不切;验证 ES 索引可写后再把
  `storage` 改 `es`。
- working tree 即部署代码(见 fila-agent-deploy 记忆),改配置即生效(restart.sh 重启)。
