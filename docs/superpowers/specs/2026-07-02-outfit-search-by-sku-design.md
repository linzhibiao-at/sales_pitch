# 搭配检索（按 SKU 检索固定搭配）设计

## 目标

在主页新增一个「搭配检索」页面：用户输入 `sku_id`，后端检索包含该 SKU 的固定搭配，前端以与「穿搭浏览」页一致的样式展示这些搭配。

## 范围

- 新增后端 HTTP 端点，按 SKU 检索固定搭配。
- 新增前端页面（HTML + JS），复用穿搭浏览页的渲染逻辑与样式。
- 在主页与穿搭浏览页加入口链接。
- 不做无限滚动、不做来源/色系 tab 筛选（见「决策」）。

## 背景

- 穿搭浏览页 `/outfits-viewer/` 通过 `GET /api/outfits` 分页拉取搭配，前端用 `viewer-common.js` 中的 `renderOutfit` 渲染每套搭配卡片。
- 后端已有 `DataFacade.outfits_by_sku(sku_id, size, sources)`（`backend/retrieval/data_facade.py:72`），ES 模式下走 `EsClient.search_outfits_by_sku`（`backend/retrieval/es_client.py:241`），本地 fallback 走 `LocalDataStore`。默认按 `OPERATIONAL_OUTFIT_SOURCES`（cc_material / micro_guide / dphs_outfits / outfits_unique）过滤，与穿搭浏览页口径一致。
- 目前没有 HTTP 端点暴露 `outfits_by_sku`，需要新增。

## 决策

1. **来源过滤**：默认全部运营来源（与 `outfits_by_sku` 默认一致），不在页面提供来源切换。
2. **不分页、不做无限滚动**：SKU 命中的固定搭配通常数量有限，一次性返回（默认 `size=100`）。若超过上限，前端提示「仅展示前 100 套」。
3. **不加来源/色系 tab**：结果已被 SKU 维度过滤，tab 价值不大，保持页面简洁。
4. **页面位置**：放在 `outfits-viewer/` 目录下（`search.html` + `search.js`），直接复用 `styles.css` 与 `viewer-common.js`，保证渲染样式与浏览页完全一致。
5. **SKU 精确匹配**：`term` 查询 `sku_ids` 字段，不做模糊/前缀匹配。

## 后端设计

### 端点

```
GET /api/outfits/by-sku?sku_id=<sku>&size=<int>
```

- `sku_id`：必填，`str`，做 `trim`，空串返回空结果（非 404）。
- `size`：可选，默认 `100`，范围 `1..500`，越界自动 clamp。

### 响应

```json
{
  "sku_id": "T61W433104F-WT",
  "size": 100,
  "total": 12,
  "outfits": [ /* 与 /api/outfits 同结构的 outfit 对象 */ ]
}
```

- `outfits` 结构与 `GET /api/outfits` 返回的一致，前端可直接复用 `renderOutfit`。
- 走 `DataFacade.outfits_by_sku`，默认 `OPERATIONAL_OUTFIT_SOURCES` 过滤。
- 对返回行做 `_enrich_outfit_color_tags` 处理（与 `browse_outfits` 一致），保证色系标签存在。
- SKU 未命中或无搭配：`total=0`、`outfits=[]`，HTTP 200。

### 实现位置

- 端点定义在 `backend/main.py`，紧邻现有 `/api/outfits` 系列。
- 复用 `_svc._data.outfits_by_sku`，无需改动 `DataFacade` / `EsClient`。

## 前端设计

### 文件

- `outfits-viewer/search.html`：页面骨架。
- `outfits-viewer/search.js`：交互逻辑。

### 页面结构

```
<header>
  FILA 搭配检索
  meta 行（结果计数 / 提示）
  header-links: 穿搭浏览 / 单套搭配
</header>

<section 搜索区>
  <input sku_id>  <button 搜索>
</section>

<section 结果区>
  <div #outfit-list>  <!-- renderOutfit 渲染 -->
  <div #result-meta>  <!-- "共 N 套包含 SKU xxx" / "仅展示前 100 套" -->
</section>
```

### 交互

- 输入框回车或点搜索按钮触发请求。
- `sku_id` 做 `trim`，空串不发请求、清空结果。
- 请求 `GET /api/outfits/by-sku?sku_id=...&size=100`。
- 用 `renderOutfit`（来自 `viewer-common.js`）逐条渲染到 `#outfit-list`。
- 结果计数：`共 N 套搭配包含 SKU xxx`；若 `N === size`，追加 `（仅展示前 N 套）`。
- 空结果：显示 `未找到包含 SKU xxx 的固定搭配`。
- 请求异常：顶部展示 `error-banner`（复用 `styles.css`）。
- URL 带 `?sku_id=xxx`：页面加载时若存在该参数则自动发起一次搜索；搜索成功后 `replaceState` 更新 URL，便于分享/刷新保持。
- 复用 `styles.css` 的 `outfit-row / items-grid / item-card` 等类，不新增样式。

### 入口链接

- `web/index.html` 的 `header-links` 增加 `<a href="/outfits-viewer/search.html">搭配检索</a>`。
- `outfits-viewer/index.html` 的 `header-links` 增加 `<a href="search.html">搭配检索</a>`。

## 测试

- 后端：`/api/outfits/by-sku` 用已知 SKU（可从 `fila_white_front_vlm.csv` 取一个）请求，断言 `total` 与 `len(outfits)` 一致、`outfits[0].items` 中存在该 SKU。
- 边界：空 `sku_id` 返回空列表；不存在的 SKU 返回 `total=0`。
- 前端：手动验证回车触发、空输入清空、URL 参数自动搜索、空结果提示、错误横幅。

## 非目标

- 不支持批量 SKU 检索（已有 `outfits_by_skus_batch`，但本次页面只做单个 SKU）。
- 不做来源/色系筛选 tab。
- 不做无限滚动 / 分页。
- 不改动穿搭浏览页现有逻辑。
