# FILA 搭配推荐使用字段说明

> 整理范围：`fila_agent_html` 商品数据、搭配预览、推荐服务及 `data/tables` 下 CSV/XLSX 字段。  
> 生成日期：2026-05-20（路径 2026-06 随 `fila/` 目录迁移更新）

---

## 1. FILA 相关项目总览

### 1.1 商品与穿搭「展示」类

| 项目路径 | 类型 | 主要用途 | 核心数据 |
|----------|------|----------|----------|
| `outfits-viewer/` | 静态 HTML/JS | 微导购固定搭配浏览：列表、搭配详情、SKU 详情 | `data/preview/fila_outfits.json`、分片 `outfits-viewer/data/chunk-*.json` |
| `image-debug-viewer/` | 静态 HTML/JS | SKU 候选图质检 | `data/image_debug_index.json`、`data/sku/{sku_id}.json`（离线生成） |
| `data/tables/` | CSV/XLSX | 商品主数据、图片、导购搭配、Hive 日更 | 见第 2～5 节 |

**展示侧关键 JSON 字段（`fila_outfits.json` / outfits-viewer）**

| 层级 | 字段 | 说明 |
|------|------|------|
| 搭配 | `idMatch` | 微导购搭配 ID（对应 `product_guide_recommend.compose_id` 或 Excel 中的搭配组） |
| 搭配 | `name`, `idShop`, `shopName`, `type`, `backgroundImg`, `leftHeroUrl` | 名称、店铺、背景/主图 |
| 搭配 | `flags.hasPdpOutfitImage`, `flags.hasCpsOutfitImage` | 是否有商详/组合搭配图 |
| 单品 | `idGoods`, `idAlias`, `title`, `isMaster` | 商品 ID、款号、标题、是否主件 |
| 单品 | `color.idPa`, `color.attrAlias`, `color.attrName` | 颜色 SKU 维度 |
| 单品 | `images.cover`, `images.swatch`, `images.outfitCd[]`, `images.outfitCps[]` | 封面、色卡、搭配图 |
| 单品 | `meta.sex`, `meta.upDown`, `meta.catType`, `meta.season`, `meta.series` | 来自 `product_master_ext` 等 |

### 1.2 搭配「推荐」类

| 项目路径 | 前端 | 后端 | 离线数据 |
|----------|------|------|----------|
| **`fila_agent_html/`** | `web/` 调试台 | `backend/` FastAPI | `data/processed/*.jsonl` |

**推荐服务数据流**

```text
data/tables/*  (Hive 日更 CSV / 业务 xlsx)
    → ETL（build_catalog / select_images / build_outfits / build_relations / build_embeddings）
    → data/processed/skus.jsonl, outfits.jsonl, compatibility_edges.jsonl, ...
    → ES (fila_skus / fila_outfits / fila_compatibility_edges)
    → Milvus (fila_sku_vectors / fila_outfit_vectors / fila_sku_text_vectors)
    → POST /chat (SSE) → web / outfits-viewer
```

配置见 `config.yaml`：`paths.product_dir: "data/tables"`。

---

## 2. 数据源文件清单（`data/tables/`）

| 文件 | 来源 | 在推荐/展示中的角色 |
|------|------|---------------------|
| `fila_table.txt` | MySQL DDL | 电商商品库表结构定义（见第 3 节） |
| `生产cc_material_product.xlsx` | 生产库 `cc_material_product` 样本导出 | 搭配明细：搭配 ID ↔ 款号（见第 4 节） |
| `微导购搭配数据.xlsx` | 业务导出 | `build_fila_guide_outfits.py` 搭配构建输入（见第 4.2 节） |
| `商品_斐乐v2.xlsx` | PDM/商品主数据 | SKU 属性、role、季节、场景、价格（见第 6 节） |
| `product_master.csv` | `product_master` 表导出 | 款号、标题、价格、主图 |
| `product_master_ext.csv` | `product_master_ext` | 性别、系列、上下装、场景等 |
| `product_attr.csv` | `product_attr` | 颜色 SKU（`attr_alias` = 货号） |
| `product_sku.csv` | `product_sku` | 店铺 SKU 价格、`mdp_sku` |
| `product_image.csv` | `product_image` | 全量图片路径与类型 |
| `product_image_type.csv` | 字典 | `wp`/`bd`/`master`/`big` 等 |
| `product_guide_recommend.csv` | 微导购搭配主表 | 固定搭配主图、导购信息 |
| `product_guide_recommend_ext.csv` | 搭配明细 | 搭配 ↔ `id_alias`（款号） |
| `fila_sku_selected_images.csv` | VLM 选图 | `tryon_image` / `index_image` 优先来源 |
| `scripts/pull_cc_material_product.py` | 脚本 | 从 MySQL 全量拉取 `cc_material_product` → CSV |

---

## 3. `fila_table.txt`（MySQL 表结构）

以下为电商侧表 DDL 摘要；**推荐 ETL 主要使用加粗表**。

### 3.1 `product_master`（商品主表）

| 字段 | 类型 | 说明 | 推荐用途 |
|------|------|------|----------|
| **id_goods** | int | 商品 ID | 关联键 → `goods_id` |
| id_shop | int | 店铺 id | 店铺维度 |
| **id_alias** | varchar(20) | **款号（SPU）** | → `spu_id`、款号解析 |
| id_brand | varchar(10) | 品牌 | 品牌 |
| **pro_title** / **pro_name** | varchar(128) | 产品标题/名 | → `title` |
| pro_info, pro_content, pro_intro | text | 详情/介绍 | 检索文本可选 |
| keyword, selling_point_label | varchar | 关键字/卖点 | 搜索 |
| cate_id | varchar(20) | 分类 | 类目 |
| min_price, max_price, **price**, market_price | decimal | 价格 | → `price`（优先） |
| onsell | int | 上下架 | 过滤在架 |
| **image** | varchar(256) | 官网主图 | → `display_image` 候选 |
| search_title | varchar(255) | 搜索标题 | ES `search_text` |

### 3.2 `product_master_ext`（商品扩展表）

| 字段 | 说明 | 推荐用途 |
|------|------|----------|
| id_goods, id_alias | 关联键 | 与主表 join |
| **sex** | 性别 | → `gender` |
| pro_season, **season**, year | 产品季/销售季 | → `season[]` |
| **series**, cat, cat_alias, **cat_type** | 系列/品类/大类 | → `series`, `category_l1` |
| **up_down** | 上下装 | → `role`（配合 v2 表） |
| material, fabric, technology | 材质/科技 | `material`, 功能 |
| **applicable_scenario** | 适用场景 | → `occasion_tags` |
| fabric_function, filling_type, down_content 等 | 功能/羽绒 | 属性过滤 |
| middle_class, short_category | 中类/筛选品类 | 类目 |

### 3.3 `product_attr`（颜色 SKU）

| 字段 | 说明 | 推荐用途 |
|------|------|----------|
| id_pa | 属性 ID | → `id_pa` |
| id_goods | 商品 ID | 关联 |
| id_pac | 属性类 | **1 = 颜色**（ETL 只取 id_pac=1 且 status=0） |
| attr_name, **attr_alias** | 色名、**货号（SKU）** | **attr_alias → sku_id** |
| image_url | 色卡图 | `display_image` / `swatch` 候选 |
| status | 0 显示 / 1 隐藏 | 过滤 |

### 3.4 `product_sku`（最小销售单元）

| 字段 | 说明 | 推荐用途 |
|------|------|----------|
| id_sku, id_goods | SKU 行 ID | 辅助 |
| shop_price, market_price | 价格 | 可兜底 `price` |
| **mdp_sku** | 最小购买单元货号 | 与 `attr_alias` 对齐校验 |
| idpas, attr_name | 属性拼接 | 展示 |

### 3.5 `product_image` / `product_image_type`

| 字段 | 说明 | 推荐用途 |
|------|------|----------|
| id_goods, id_pa | 关联商品/颜色 | 按色选图 |
| **path** | 图片 URL | display/index/tryon 候选池 |
| **image_type** | 类型 | 配合 `product_image_type`：`wp`/`master`/`big` 等 |
| order_id, status | 排序、启用 | 选图优先级 |

### 3.6 `product_guide_recommend` / `product_guide_recommend_ext`（微导购固定搭配）

| 表 | 字段 | 说明 | 推荐用途 |
|----|------|------|----------|
| 主表 | **compose_id** | 微导购 compose ID | → `outfit_id` 后缀 / `id_match` |
| 主表 | guide_name, staff_num, area | 导购信息 | → `guide_name`, `area` |
| 主表 | **image**, other_image | 搭配封面/扩展图 | → `display_image`, `index_image` |
| 主表 | status | 是否推荐 | 过滤 |
| 明细 | id_guide_recommend | 关联主表 id | join |
| 明细 | **id_alias** | **款号** | 解析到 `sku_id`（经 `product_attr`） |

### 3.7 其他表（检索/搜索，推荐间接使用）

- `search_index`：搜索关键词 `s_1`～`s_30`、`goods_sn`、`colors_atla` 等。
- `product_image_type`：`image_type` ↔ `image_type_name`。

---

## 4. `cc_material_product` 与 `生产cc_material_product.xlsx`

### 4.1 生产 MySQL 表（`ry-cloud.cc_material_product`）

脚本：`scripts/pull_cc_material_product.py`  
主键：`material_product_id`

| 字段 | 说明 | 推荐/ETL 用途 |
|------|------|----------------|
| **material_product_id** | 搭配明细行 ID | 搭配内单品排序、去重 |
| material_id | 物料/搭配组 ID | 与 `material_product_id` 组合标识一套搭配 |
| **article_no** | **款号（SPU）** | 经 `product_master.id_alias` → 颜色 `attr_alias` → **sku_id** |
| product_name | 产品名称 | 展示名兜底 |
| create_by, create_time, modify_by, modify_time | 审计字段 | 一般不入推荐索引 |

> 全量列以线上 `information_schema` 为准；脚本默认列见 `pull_cc_material_product.py` 中 `DEFAULT_COLUMNS`。

### 4.2 仓库内 `生产cc_material_product.xlsx`（当前快照）

| Sheet | 列 | 说明 |
|-------|-----|------|
| Sheet1 | `material_product_id` | 搭配明细 ID 列表 |
| Sheet2 | `article_no` | 款号列表 |

**说明**：当前仓库中的 xlsx 为**分列样本**（两表各一列），未包含 `material_id`、`product_name` 等完整宽表。完整关系需以 MySQL 导出或 `cc_material_product_all.csv` 为准。  
行级对应关系：`material_product_id` 与 `article_no` 按行序或需通过 `material_id` 在库内 join（生产环境以库表为准）。

### 4.3 `微导购搭配数据.xlsx`

`scripts/build_fila_guide_outfits.py` **设计的列序**（`load_xlsx_rows` 读取前 8 列）：

| 列序 | 字段名 | 说明 |
|------|--------|------|
| 0 | material_product_id | 搭配明细 ID |
| 1 | id_match（搭配组 ID） | 微导购搭配 ID → `idMatch` |
| 2 | article_no | 款号 → SKU 解析 |
| 3 | product_name | 产品名（展示兜底） |

**仓库内现状**：`微导购搭配数据.xlsx` 当前仅含 `material_product_id` 一列（样本不完整）。生产搭配构建应使用完整导出或与 `product_guide_recommend*.csv` 对齐。

### 4.4 款号 → SKU 解析规则（展示 & 推荐共用）

1. `article_no` / `id_alias` 查 `product_master.id_alias` → `id_goods`
2. 在 `product_attr`（`id_pac=1`, `status=0`）中选 `attr_alias`：优先完全匹配款号，否则前缀匹配
3. `attr_alias` 即 **`sku_id`**（货号）；款号去掉颜色后缀为 **`spu_id`**

---

## 5. `data/tables` CSV 导出字段（与 `fila_table.txt` 对应）

### 5.1 `product_master.csv`（42 列）

`id_goods`, `id_shop`, `id_alias`, `id_brand`, `pro_title`, `pro_name`, `pro_info`, `pro_content`, `pro_intro`, `keyword`, `selling_point_label`, `cate_id`, `min_price`, `max_price`, `price`, `market_price`, `division_price`, `add_time`, `up_time`, `market_time`, `ficti`, `sales`, `sales_week`, `sales_month`, `w_order`, `onsell`, `mem_type`, `image`, `video`, `pre_switch`, `pre_desc`, `pre_price`, `updated_at`, `is_promotions_goods`, `promotions_info`, `bind_id`, `share_count`, `is_hide`, `is_lock`, `is_booking_pay_full`, `is_live`, `search_title`

**推荐高频字段**：`id_goods`, `id_alias`, `pro_title`/`pro_name`, `price`, `image`, `onsell`

### 5.2 `product_master_ext.csv`（50 列）

`id_goods`, `id_shop`, `id_alias`, `sex`, `pro_season`, `series`, `cat`, `cat_code`, `cat_alias`, `cat_type`, `up_down`, `up_date`, `order_type`, `year`, `season`, `length`, `thickness`, `weav`, `material`, `fabric`, `navigation`, `modeling`, `craft`, `standard`, `age`, `class`, `technology`, `other`, `cate_id`, `add_time`, `update_time`, `size_table`, `maintenance`, `middle_class`, `short_category`, `size_category`, `filling_type`, `down_content`, `filling_weight`, `fabric_function`, `moisture_permeability_index`, `waterproof_index`, `grade_segment`, `weight`, `capacity`, `functional_tag`, `applicable_scenario`, `upper_function`, `midsole_function`, `outsole_function`

### 5.3 `product_attr.csv`（9 列）

`id_pa`, `id_goods`, `id_pac`, `attr_name`, `attr_alias`, `order_id`, `image_url`, `video_url`, `status`

### 5.4 `product_sku.csv`（10 列）

`id_sku`, `id_shop`, `id_goods`, `shop_price`, `market_price`, `image`, `idpas`, `attr_name`, `title`, `mdp_sku`

### 5.5 `product_image.csv`（7 列）

`id_pi`, `id_pa`, `id_goods`, `path`, `order_id`, `image_type`, `status`

### 5.6 `product_guide_recommend.csv`（11 列）

`id`, `compose_id`, `guide_name`, `staff_num`, `area`, `image`, `other_image`, `status`, `create_time`, `update_time`, `delete_time`

### 5.7 `product_guide_recommend_ext.csv`（5 列）

`id`, `id_guide_recommend`, `id_alias`, `create_time`, `update_time`

---

## 6. `商品_斐乐v2.xlsx`（203 列，推荐 ETL 重点）

完整列名见 PDM 导出；下表为 **`fila_agent_html` 技术方案已确认** 与推荐强相关字段。

| 业务含义 | Excel 列名 | 映射到 processed |
|----------|------------|------------------|
| SKU 货号 | `货号` | `sku_id` |
| SPU 款号 | `款号` | `spu_id` |
| 商品名 | `产品名称`、`产品名`、`品名`、`电商款名` | `title` |
| 大类/中类/小类 | `大类`、`中类`、`小类`、`电商中类`、`分析中类`、`分析小类` | `category_l1/l2/l3` |
| 穿搭角色 | **`上下装`**、`裤型` | **`role`**（top/bottoms/shoes/dress/accessory） |
| 人群 | `性别`、`年龄段`、`性别-吊牌` | `gender` |
| 季节 | `开发季`、`销售年度`、`季节`、`季节属性`、`配货季` | `season[]` |
| 系列 | `系列`、`子系列`、`分析系列`、`系列（吊牌）` | `series`, `sub_series` |
| 价格 | **`订货会零售价`**、`价格带` | `price`（主表缺失时兜底） |
| 场景风格 | `场景`、`场景_标签`、`子场景`、`风格`、`颜色风格` | `occasion_tags`, `style_tags` |
| 颜色 | `颜色`、`颜色文本`、`颜色色系` | `color_name`, `color_family` |
| 功能材质 | `面料功能性`、`客观功能`、`认知功能`、`主成分`、`面材料/成分` | `fabric_function`, `material` |

**Role 归一规则**（来自 `技术方案.md`）：

| 来源 | role |
|------|------|
| 上下装=上装 | `top` |
| 上下装=下装 | `bottoms` |
| 上下装=连衣裙 | `dress` |
| 大类=鞋类 | `shoes` |
| 大类=配件 | `accessory` |
| 无法判断 | `unknown` |

---

## 7. `fila_agent_html` 离线推荐模型字段（`data/processed/`）

### 7.1 `skus.jsonl`（单品，一条一 SKU）

| 字段 | 类型 | 来源字段摘要 | 用途 |
|------|------|--------------|------|
| **sku_id** | string | `货号` / `product_attr.attr_alias` | 主键、Milvus/ES |
| **spu_id** | string | `款号` / `product_master.id_alias` | 同款聚合、锚点泛化 |
| goods_id | int | `id_goods` | 关联商品 |
| id_pa | int | `product_attr.id_pa` | 颜色维度 |
| title | string | `pro_title` + v2 名称 | 展示、理由 |
| brand | string | 固定 FILA | 检索 |
| gender | string | `sex` / v2 `性别` | 过滤、排序 |
| category_l1/l2/l3 | string | v2 大类/中类/小类 | 类目 |
| **role** | string | v2 `上下装` + 大类规则 | 召回目标角色 |
| up_down_raw | string | ext.`up_down` | 原始上下装 |
| series, sub_series | string | ext / v2 | 系列匹配 |
| season | string[] | ext / v2 | 季节匹配 |
| occasion_tags, style_tags | string[] | v2 场景/风格 | 意图过滤 |
| color_name, color_family | string | v2 颜色 | 配色 |
| **price** | float | `product_master.price` 或 v2 `订货会零售价` | 总价、预算 |
| display_image | string | `wp`/`big`/`master`/主图 | 卡片展示 |
| **index_image** | string | 白底正面 VLM/`master` | **Milvus 单品向量（必填）** |
| tryon_image | string | 同 index 标准，可空 | 虚拟试衣 |
| image_quality | object | ETL 打分 | `is_tryon_ready` |
| **search_text** | string | 拼接品牌/性别/系列/标题/role/sku | **ES 文本召回** |

### 7.2 `outfits.jsonl`（固定搭配）

| 字段 | 类型 | 来源 | 用途 |
|------|------|------|------|
| **outfit_id** | string | `guide_{compose_id}` | 主键 |
| name | string | 主件标题或导购名 | 展示 |
| source | string | `micro_guide` | 召回来源标识 |
| guide_name, area | string | `product_guide_recommend` | 元信息 |
| status | int | 主表 status | 过滤 |
| **items[]** | array | 明细 + SKU 解析 | 套装单品 |
| items[].sku_id, spu_id, role | string | 同上 | 卡片、关系边 |
| items[].is_anchor | bool | 用户锚点标记 | 锚点高亮 |
| items[].title, price, display_image, tryon_image | — | SKU 文档 | 展示 |
| roles | string[] | items 聚合 | 完整度 |
| gender, season, series_tags, occasion_tags, style_tags, color_palette | — | 聚合 SKU | 排序 |
| **price_total** | float | items 求和 | 展示 |
| display_image, index_image | string | 导购图或主件图 | 搭配卡片/向量 |
| tryon_coverage, outfit_completeness_score, quality_score | float | ETL 计算 | 排序 |
| search_text | string | 拼接 | ES 搭配召回 |

### 7.3 `compatibility_edges.jsonl`（互补关系）

| 字段 | 说明 |
|------|------|
| relation_id | `{outfit_id}:{source_sku}->{target_sku}` |
| outfit_id | 溯源套装 |
| source_sku_id, target_sku_id | 有向边端点 |
| source_role, target_role | 角色 |
| cooccur_score, color_match_type | 共现与配色 |
| gender, season, series_tags, occasion_tags, style_tags | 边属性 |

### 7.4 索引文件

| 文件 | 内容 |
|------|------|
| `sku_to_outfits.json` | `sku_id` → `[outfit_id, ...]` |
| `spu_to_skus.json` | `spu_id` → `[sku_id, ...]` |

---

## 8. `fila_agent_html` API 入参 / 出参（推荐接口）

详细说明见：[README.md](../README.md) 第 7 节 API。

### 8.1 请求 `POST /chat`

| 字段 | 必填 | 说明 |
|------|------|------|
| session_id | 否 | 多轮会话 |
| message | 否* | 用户文本 |
| image_base64 | 是* | 上传图（纯 Base64） |
| selected_sku_id | 否 | 锚点 SKU（最高优先级） |
| selected_spu_id | 否 | 锚点 SPU（取首个 SKU） |

\* 至少提供 `message`、`image_base64`、`selected_sku_id` 之一。

### 8.2 响应 `outfits[]` / `items[]`（SSE `outfit_results`）

| 层级 | 核心字段 | 对应 processed |
|------|----------|----------------|
| 套装 | outfit_id, id_match, name, recall_source, is_synthetic | outfits.jsonl |
| 套装 | display_image, index_image, price_total, reason | 同上 |
| 套装 | outfit_completeness_score, tryon_coverage, quality_score | 排序分 |
| 单品 | sku_id, spu_id, role, title, price | skus.jsonl |
| 单品 | display_image, tryon_image, is_anchor, is_master | 卡片 |

---

## 9. 检索存储字段（ES / Milvus）

| 存储 | 索引/集合 | 主要字段 |
|------|-----------|----------|
| Elasticsearch | `fila_skus` | `sku_id`, `search_text`, `gender`, … |
| Elasticsearch | `fila_outfits` | `outfit_id`, `search_text`, … |
| Elasticsearch | `fila_compatibility_edges` | `source_sku_id`, `target_sku_id`, … |
| Milvus | `fila_sku_vectors` | `sku_id`, `spu_id`, `product_vector`(1024), `index_image` |
| Milvus | `fila_outfit_vectors` | `outfit_id`, `sku_ids`, `product_vector` |
| Milvus | `fila_sku_text_vectors` | 文本向量拼套召回 |

---

## 10. 字段映射总表（推荐链路）

```text
cc_material_product.article_no  ─┐
product_guide_recommend_ext.id_alias ─┤→ product_master.id_alias (SPU)
商品_斐乐v2.款号                    ─┘         ↓
                                    product_attr.attr_alias (SKU)
                                              ↓
                                    skus.jsonl (sku_id, spu_id, role, price, images…)
                                              ↓
                         ┌────────────────────┼────────────────────┐
                         ↓                    ↓                    ↓
                   outfits.jsonl    compatibility_edges.jsonl   ES / Milvus
                         ↓
              POST /chat → card_builder → web 展示 / outfits-viewer
```

| 业务概念 | 生产/电商库 | 离线 processed | API 出参 |
|----------|-------------|----------------|----------|
| 货号 SKU | `attr_alias` / v2.`货号` | sku_id | items[].sku_id |
| 款号 SPU | `id_alias` / v2.`款号` | spu_id | items[].spu_id |
| 搭配 ID | compose_id / material 组 | outfit_id, id_match | outfit_id, id_match |
| 搭配明细行 | material_product_id | （构建中间字段） | — |
| 展示图 | product_image + 类型 | display_image | display_image |
| 检索/试衣图 | fila_sku_selected_images | index_image, tryon_image | tryon_image |
| 角色 | v2.上下装 + 大类 | role | role |
| 价格 | product_master.price / v2.订货会零售价 | price, price_total | price, price_total |

---

## 11. 相关文档与脚本索引

| 路径 | 说明 |
|------|------|
| [README.md](../README.md) | 环境、数据流水线、API、启动 |
| [FILA搭配推荐使用字段.md](FILA搭配推荐使用字段.md) | 本文档 |
| `scripts/build_fila_guide_outfits.py` | 搭配预览 JSON 构建 |
| `scripts/pull_cc_material_product.py` | 生产表全量导出 |
| `outfits-viewer/` | 搭配静态展示 |

---

## 12. 数据质量备注

1. **仓库内** `生产cc_material_product.xlsx`、`微导购搭配数据.xlsx` 为**窄表样本**，与生产宽表、脚本预期列序可能不一致；联调请以 MySQL/`cc_material_product_all.csv` 及完整业务导出为准。  
2. **价格**：优先 `product_master.price`，缺失用 `商品_斐乐v2.xlsx` 的 `订货会零售价`。  
3. **Milvus 单品向量**：`index_image` 不能为空；否则跳过向量入库。  
4. **固定搭配**：可用单品 &lt; 2 的套装在 ETL 中跳过，不作为首屏推荐。
