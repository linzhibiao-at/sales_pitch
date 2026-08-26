# 2026-07-09 — FILA Milvus Hybrid 检索复刻 descent + 源头字段补齐

## 背景与目标

参考实现 `/home/jovyan/swap/tmp/descent_product_search` 用 **Milvus 原生 BM25**(search_text 字段开 chinese analyzer + `Function(BM25)` 自动产 sparse_vector)+ **dense 向量**(IVF_FLAT/COSINE)+ **`MilvusClient.hybrid_search`**(`AnnSearchRequest` ×2 + `RRFRanker`/`WeightedRanker`)做单集合关键词+语义混合检索。

当前 fila 项目:三个 dense-only 集合(`fila_sku_vectors` 图 / `fila_sku_text_vectors` 文 / `fila_sku_complementary_vectors` 互补),运行时用**旧 ORM** API;关键词检索靠 ES。要做的是:**复刻 descent 的 hybrid 实现(函数改名),并从 catalog 源头补齐 descent 用到而 fila 缺失的字段,使下游 ES 与 Milvus 都能用上**。

云集群实测 `server_version=2.6.3`,pymilvus 2.6.13,支持 BM25 Function / hybrid_search / RRF/Weighted ranker。`pymilvus.Function/FunctionType/AnnSearchRequest/RRFRanker/WeightedRanker` 可导入。

## 已定决策(来自问答)

1. **集成方式**:新建 `backend/retrieval/hybrid_search.py`(检索)+ `scripts/build_hybrid_index.py`(建索引),并接入 `SkuRetriever` 作为文本召回新通路,旧 dense 通路(`recall_by_text_vector_keywords`)留作 fallback。
2. **目标集合**:新建 `fila_sku_hybrid_vectors`(config 可配),不动线上 `fila_sku_text_vectors`,零召回空窗。
3. **命名风格**:fila 风格 —— `FilaSkuHybridSearcher` / `search_keyword` / `search_semantic` / `search_hybrid` / `get_skus_by_ids` / `build_keyword_text` / `build_semantic_text` / `build_hybrid_schema` / `create_hybrid_collection` / `rewrite_query` / `rule_rewrite` / `llm_rewrite`。
4. **源头补字段**:从 `etl_common.build_sku_record`(build_catalog 的底层构造点)补齐 descent 字段。

## 命名对照(descent → fila)

| descent | fila(改名) |
|---|---|
| `SearchEngine` | `FilaSkuHybridSearcher` |
| `keyword_search` | `search_keyword` |
| `semantic_search` | `search_semantic` |
| `hybrid_search` | `search_hybrid` |
| `get_products_by_ids` | `get_skus_by_ids` |
| `build_search_text`(data_processor) | `build_keyword_text` |
| `build_semantic_text` | `build_semantic_text` |
| `build_schema`/`get_index_params`/`create_collection` | `build_hybrid_schema`/`get_hybrid_index_params`/`create_hybrid_collection` |
| `rewrite_query`/`rule_rewrite`/`llm_rewrite` | 同名保留 |

## 架构:三段式

### Part 1 — Catalog 源头补字段

**注入点**:`scripts/etl_common.py::ProductTables.build_sku_record`(build_catalog.py 调用它,改这一处下游全受益)。

在现有 return dict 追加以下字段(源列已在 `product_master`(30 列)/`product_master_ext`(50 列)/`product_attr` 加载):

| 新字段 | 源列 | 类型/处理 |
|---|---|---|
| `product_name_short` | master.`pro_name` | text_or_empty |
| `goods_sn` | master.`id_alias` | text_or_empty(search_index.csv 空,用 id_alias 兜底,与 descent 一致) |
| `brand_line` | master.`id_brand` | map {1:FILA主,17:FILA KIDS,21:FILA FUSION,10:联名};复用 build_catalog `_FILA_BRAND_IDS` |
| `market_price` | master.`market_price` | safe_float |
| `min_price` | master.`min_price` | safe_float |
| `max_price` | master.`max_price` | safe_float |
| `year` | ext.`year` | text_or_empty |
| `category` | ext.`cat_alias` | text_or_empty(与 category_l2 口径略不同) |
| `length` | ext.`length` | text_or_empty(原始值,区别于派生 length_class) |
| `technology` | ext.`technology` | text_or_empty |
| `features` | merge(master.`pro_info`, master.`pro_content`) | descent `merge_features` 逻辑(逗号合并去重) |
| `selling_point_label` | master.`selling_point_label` | text_or_empty |
| `keyword` | master.`keyword` | text_or_empty(原始,现有只折进 search_keywords) |
| `color_images` | attr.`image_url` 按 color 聚合 | JSON 字符串(descent 同) |
| `video_url` | master.`video` | text_or_empty |
| `onsell` | master.`onsell` | int(原始,现有只算 is_onsell 布尔) |
| `sales` | master.`sales` | int |
| `sales_week` | master.`sales_week` | int |
| `sales_month` | master.`sales_month` | int |
| `w_order` | master.`w_order` | int |
| `sku_count` | product_sku 按 id_goods 计数 | int(ProductTables 一次计数预建 map) |

**`cat_type`** = 现有 `category_l1`(=ext.cat_type),不重复存,build 侧直接映射。

**无源列、跳过**:`is_hide`(fila master 无此列)。

- `build_search_text`(fila 现有,etl_common:1201)**保持不动**,避免影响既有 ES 搜索文本与行为;新字段供下游 build 各自取用。
- 数值统一过 safe_float/safe_int(复用 descent 的 `_safe_float/_safe_int` 风格或 fila 既有 helper),文本截断按各字段长度。
- `sku_count`:在 `ProductTables` 加载期按 `product_sku.id_goods` 预建 `sku_count_by_goods` map,build_sku_record 查表。
- `build_catalog.py` 覆盖率统计追加:brand_line / year / features / selling_point_label / sales / goods_sn 等项。

### Part 2 — 共享文本模块(新建)

**`scripts/hybrid_text.py`**:被 ES build 与 Milvus build 共用,避免重复。

```python
def build_keyword_text(row: dict) -> str   # = descent build_search_text 的 fila 版
    # search_keywords + keyword + title×3(加权) + product_name_short + brand_line +
    # series + sub_series + category + cat_type(=category_l1) + up_down_raw +
    # gender(列表展开) + age + season(列表展开) + year + modeling + length +
    # material(fabric) + technology + features + selling_point_label + color_name + goods_sn

def build_semantic_text(row: dict) -> str  # = descent build_semantic_text 的 fila 版
    # title + "品牌线:brand_line 系列:series 品类:category 性别:gender …" key:value
```

- 标题重复 3 次做 BM25 权重 boost(同 descent)。
- 空段自动跳过。

### Part 3 — ES build 消费新字段

**`scripts/build_fila_es_index.py`**:

- `create_skus_index` mapping `properties` 追加:
  - `brand_line` / `year` / `goods_sn` / `category` / `length` / `modeling`(已有)/ `color_images` → keyword(object/nested)
  - `market_price` / `min_price` / `max_price` → double
  - `onsell` / `sales` / `sales_week` / `sales_month` / `w_order` / `sku_count` → integer
  - `features` / `selling_point_label` / `technology` / `keyword` / `product_name_short` → ik text(`ik_max_word`/`ik_smart`)
- `sku_doc(row)`:`search_text` 改为 `build_keyword_text(row)`(富化,含 features/selling_point/technology/keyword/brand_line),其余字段照常写入 _source。
  - 既有 `search_text` 字段名与 mapping 不变,仅内容富化。
- 可选:expr 过滤支持 year/brand_line/price 区间(对应 descent `build_filter_expr`)——留作运行时按需接入,本 spec 仅建好索引字段。

> ES mapping 变更需 `--reset` 重建索引,与 Milvus 重建一并排期。

### Part 4 — Milvus hybrid build(新建)

**`scripts/build_hybrid_index.py`**:复刻 descent 的 schema_manager + build_index。

- `build_hybrid_schema(client)` → `CollectionSchema`:
  - `sku_id` VARCHAR(64) PK
  - `search_text` VARCHAR(8192) **enable_analyzer=True, analyzer_params={'type':'chinese'}**
  - `sparse_vector` SPARSE_FLOAT_VECTOR(BM25 Function 自动产)
  - `dense_vector` FLOAT_VECTOR(dim=`config.embedding.dimensions`)
  - 标量(供 expr 过滤,复用 fila 现有 + 新增):product_name / color_series(ARRAY) / color_name / category_l2 / role / gender(ARRAY) / season / layer / coverage / length_class / is_intimate / scene_domain / series / group_brand / modeling / price / age / up_time / id_goods + **新增 year / brand_line / market_price / features / selling_point_label / technology / goods_sn / onsell / sales / sku_count**
  - `Function(name='search_text_bm25', input_field_names=['search_text'], output_field_names=['sparse_vector'], function_type=FunctionType.BM25)`
  - `enable_dynamic_field=False`
- `get_hybrid_index_params()`:
  - `sparse_vector`: SPARSE_INVERTED_INDEX / BM25 / {drop_ratio_build:0.2}
  - `dense_vector`: IVF_FLAT / COSINE / {nlist:64}(复刻 descent;非 fila 现有 HNSW)
  - `up_time` / `group_brand`: INVERTED(沿用 fila)
- `create_hybrid_collection(client, name, dim)`:`create_collection(schema, index_params)` 一次落(不单独 create_index)。
- 灌数据:遍历 `data/processed/skus.jsonl` → `build_keyword_text(row)`→search_text 原文;`build_semantic_text(row)`→`embed_text`(fila embedding_client,ARK/DashScope)→dense_vector;sparse_vector **不填**(服务端 BM25 Function 自动产);`client.insert` 分批 500 → `flush`。
- CLI:沿用 build_text 风格 `--reset / --incremental / --prune-orphans / --limit / --batch-size`;状态文件 `fila_index_sync_state.json` 的 `milvus.sku_hybrid_vectors` bucket。
- **local(Milvus Lite *.db)检测**:BM25 Function 可能不支持,local uri 下明确报错而非静默。

### Part 5 — 运行时 hybrid 检索(新建 + 接入)

**`backend/retrieval/hybrid_search.py`**:复刻 descent `search_engine.py`。

```python
class FilaSkuHybridSearcher:
    # MilvusClient(uri=..., token=...) 懒加载,读 config.milvus(沿用 stash/restore pymilvus uri 的套路)
    def search_keyword(self, query, *, expr=None, limit, output_fields) -> list[hit]
        # client.search anns_field='sparse_vector', data=[query 文本], metric BM25
    def search_semantic(self, query, *, expr=None, limit, output_fields) -> list[hit]
        # embed_text(query) → client.search anns_field='dense_vector', metric COSINE, nprobe
    def search_hybrid(self, query, *, expr=None, limit, kw_w, sem_w, ranker, output_fields) -> list[hit]
        # AnnSearchRequest(sparse, 文本, BM25) + AnnSearchRequest(dense, vec, COSINE)
        # + RRFRanker(k=60) 或 WeightedRanker(kw_w, sem_w) → client.hybrid_search
    def get_skus_by_ids(self, ids, output_fields) -> list[dict]
        # client.query filter='sku_id in [...]'

# query 改写(jieba 规则 + 可选 LLM,LLM 默认关;fila 关键词来自 intent 层,接入时 skip_rewrite)
def rewrite_query(q, existing_filters=None) -> RewriteResult
def rule_rewrite(q, existing_filters=None) -> RewriteResult
def llm_rewrite(q) -> RewriteResult | None
```

**`backend/retrieval/sku_retriever.py` 接入**:新增 `recall_by_hybrid(...)`,搬 `recall_by_text_vector_keywords` 的多关键词 + color_series strict/relaxed auto 回退 + category_l2 空命中回退逻辑,底层换成"每关键词一次 `search_hybrid` → 按 sku 取 max score":
- 复用现有 expr 构造器(`build_category_l2_milvus_expr` / `_build_color_series_expr` / `build_up_time_milvus_expr` / `build_group_brand_milvus_expr` / `merge_milvus_expr`)。
- `hit_to_similarity` 扩展:RRF/Weighted 分数非 COSINE 量纲,**hybrid 通路默认 min_sim=0**(阈值调参留 TODO,先关过滤)。
- 0 命中 → fallback 调 `recall_by_text_vector_keywords`(旧 dense 通路)。

## 配置(`config.yaml` milvus 段追加)

```yaml
  collections:
    sku_hybrid_vectors: "fila_sku_hybrid_vectors"   # 新增
  hybrid:                                            # 新增
    keyword_weight: 0.2
    semantic_weight: 0.8
    ranker: rrf            # rrf | weighted
    default_limit: 20
    nprobe: 16
```

embedding 沿用 `config.embedding`(model/dimensions/api_key_env);build 与 runtime 都用 `backend.embedding_client.embed_text`,dim 一致。

## 不动的东西(明确 out of scope)

- ES `recall_by_text` 关键词通道逻辑不改(仅 ES 索引字段富化)。
- `fila_sku_vectors`(图)/`fila_sku_complementary_vectors`(互补)不动。
- `backend/retrieval/milvus_client.py` 旧 ORM 客户端保留(图/互补仍在用),不重写。
- 现有 `fila_sku_text_vectors` 集合不动,新集合并存直到切量。

## 关键风险与约束

1. **dense 模型一致性**:build 与 runtime 都用 fila `embed_text`,dim 取 `config.embedding.dimensions`;不能混 descent 的 DashScope 直连。
2. **RRF 分数与 min_sim 阈值不同量纲**:hybrid 通路先关 min_sim 过滤,待 eval 调参。
3. **集合重建**:新集合 `fila_sku_hybrid_vectors` 全新,无线上空窗;`--reset` 只影响新集合。ES mapping 变更需 `--reset` 重建。
4. **Milvus Lite(local *.db)BM25 Function 可能不支持**:build 脚本 local uri 下明确报错。
5. **新字段空值较多**:coverage 报告会显示;BM25 文本空段自动跳过,不影响检索。
6. **sku_count** 需 product_sku 全表按 gid 计数,加载期 O(n) 一次预建 map。

## 构建/运行序列(上线排期)

1. 改 `etl_common.build_sku_record` 补字段 → 跑 `build_catalog.py` 重建 `skus.jsonl`(全量)。
2. `build_fila_es_index.py --reset` 重建 ES skus 索引(mapping 含新字段 + 富化 search_text)。
3. `build_hybrid_index.py --reset` 建 `fila_sku_hybrid_vectors` 集合并灌数据。
4. 跑 `build_hybrid_index.py --limit N` smoke 验证 search_hybrid 可检索。
5. 部署 `hybrid_search.py` + `sku_retriever.recall_by_hybrid`,灰度切量(主通路 hybrid,旧 dense fallback)。
6. eval 调参(min_sim 阈值 / kw_w / sem_w / ranker)。

## 文件清单

**新建**:
- `scripts/hybrid_text.py` — 共享 `build_keyword_text` / `build_semantic_text`
- `scripts/build_hybrid_index.py` — Milvus hybrid 集合建索引 + 灌数据
- `backend/retrieval/hybrid_search.py` — `FilaSkuHybridSearcher` + rewrite

**修改**:
- `scripts/etl_common.py` — `build_sku_record` 补 22 字段 + `sku_count` map
- `scripts/build_catalog.py` — coverage 统计追加新字段项
- `scripts/build_fila_es_index.py` — mapping 追加字段 + `sku_doc` 富化 search_text
- `backend/retrieval/sku_retriever.py` — 新增 `recall_by_hybrid`
- `config.yaml` — milvus.collections.sku_hybrid_vectors + milvus.hybrid
