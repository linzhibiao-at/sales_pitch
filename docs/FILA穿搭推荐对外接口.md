# FILA 穿搭推荐对外接口文档

> 对外调用方接口，按 `docs/FILA穿搭推荐入参出参.md` 定义实现。
> 服务地址：`http://<host>:8888`（生产 8 worker，uvicorn）。
> 所有接口 `Content-Type: application/json`，响应体均为 JSON。

> **鉴权（`config.auth.enabled=true` 时强制，仅作用于对外接口）**：`POST /v1/outfit/recommend` 与 `POST /v1/outfit/regenerate-reason` 须在请求头携带 `X-API-Key: ak_<32位hex>`，Key 在 `config/api_keys.yaml` 白名单内且 `status=active`、未过期；请求体 `app_id` 须与 Key 绑定的 `app_id` 一致。详见 `docs/FILA接口鉴权与限流方案.md`。`enabled=false`（默认）时不强制。限流为进程内实现（QPM/日量硬限制 + 并发排队软限制），超限返 429。
> `GET /api/outfits`、`/skus/{sku_id}`、`/spus/{spu_id}/skus`、`/outfits/{outfit_id}`、`/api/search-debug/*`、`/api/ui-config` 等为**一向前端/调试接口**（浏览器无法持有 API Key），**不强制鉴权**，由网络层 ACL 兜底防爬取。

### 错误响应统一格式

任何错误（400 / 422 / 4xx / 5xx）均返回同一 envelope，HTTP 状态码 = `code`：

```json
{ "code": 400, "message": "app_id required", "trace_id": "4767d73c23a147b4bfd677106727f12c" }
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | int | HTTP 状态码（与响应行一致） |
| `message` | string | 人可读错误原因；422 为字段级校验压成一行（如 `app_id: Field required`），5xx 形如 `<异常类型>: <原因>`（截断 500 字） |
| `trace_id` | string | 服务端生成的 `uuid4().hex`，用于与日志关联排查；**真实堆栈仅写日志，不外泄** |

> 调用方按 `code` 判断错误类别。注意：**不再返回旧的 `{"detail": ...}` 字段**，已按 `detail` 解析的调用方需改读 `code` / `message` / `trace_id`。

> 所有响应（成功与错误）均带 `X-Trace-Id` 响应头，值与成功 body 内的 `trace_id` / 错误 envelope 的 `trace_id` 同源，便于在网关/调用方日志侧直接按 header 取用。

---

## 1. 搭配推荐接口

### `POST /v1/outfit/recommend`

根据锚点 SKU / 用户图 / 文字需求，返回最多 6 套搭配（含每套单品与推荐理由、试穿图）。

### 1.1 请求参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `session_id` | string | 否 | 多轮会话 ID；不传服务端生成 `uuid4().hex` |
| `app_id` | string | **是** | 调用方应用 id；须在 `config.recommend.allowed_app_ids` 白名单内（默认 `micro_guide`/`wechat_mini`），非白名单返 401 `invalid app_id`（大小写敏感）；鉴权开启时还须与 `X-API-Key` 绑定的 `app_id` 一致，否则 401 `app_id mismatch with API key` |
| `message` | string | 否 | 用户文字需求描述 |
| `image_url` | string | 否* | 用户上传图片 URL；服务端抓取转 base64。仅图无 sku 时图作为锚点 |
| `input_sku_id` | string | 是* | 已选锚点 SKU 货号（优先级最高），只传一个；须符合基本格式 `^[A-Z][0-9][A-Z0-9-]*$`，否则返 400 `invalid sku_id format`（格式合法但查无此 SKU 仍 200 返回空 outfits） |
| `tryon` | bool | 否 | 是否试穿，默认 `false`；**严格布尔**，`1`/`0`/`"true"`/`"false"` 等隐式转换一律 422，避免误触发试穿（22s+） |
| `reason_style` | string | 否 | 话术风格（当前透传暂不生效，预留，不校验取值） |

> \* `input_sku_id` / `image_url` / `message` **至少传一个**；`app_id` 必填。两条不满足均返回 400/422。

> 试穿模特图 male/female/boy/girl 已写在 `config.yaml` → `recommend.tryon.person_images`。

### 1.2 请求示例

```json
{
  "session_id": "a1b2c3d4e5f6789012345678abcdef01",
  "app_id": "micro_guide",
  "message": "这件浅灰 T 恤怎么搭？",
  "input_sku_id": "F11W619219FPK",
  "tryon": false
}
```

仅图无 sku（图作为锚点）：

```json
{
  "app_id": "micro_guide",
  "image_url": "https://img.fishfay.com/shopgoods/7/F11W621101F/F11W621101FWT/11/61b133e2da621434d2b09b5055b8066d.jpg"
}
```

### 1.3 响应

#### 顶层字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `session_id` | string | 会话标识 |
| `input_sku_id` | string | 输入 sku_id（仅图无 sku 时为空串） |
| `outfits` | list | 推荐套装列表，默认最多 6 套（`config.recommend.default_outfit_limit`） |
| `trace_id` | string | 服务端生成的 `uuid4().hex`，与错误 envelope 同源；凭此查服务端日志联调 |

#### `outfits[]` 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `outfit_id` | string | 搭配 id |
| `outfit_rank` | int | 搭配排序（0-based） |
| `items` | list | 搭配的单品 json 信息 |
| `outfit_tryon_image` | string | 试穿图 URL（`tryon=false` 时可能为空） |
| `reason` | string | 推荐理由（约 300 字） |

#### `items[]` 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `sku_id` | string | 单品货号 |
| `spu_id` | string | SPU 货号 |
| `id_goods` | string | 官网 goods id |
| `role` | string | 单品角色（top / bottoms / shoes …） |
| `title` | string | 单品名称 |
| `price` | float | 单品价格 |
| `sku_image_url` | string | 单品图 URL |

> `sku_image_url` 规则：真实 SKU 取该单品 `tryon_image` URL；被上传图覆盖时回退真实图；**仅图无 sku 的虚拟图锚点取入参 `image_url`**。

### 1.4 响应示例

```json
{
  "session_id": "a1b2c3d4e5f6789012345678abcdef01",
  "input_sku_id": "F11W619219FPK",
  "trace_id": "4767d73c23a147b4bfd677106727f12c",
  "outfits": [
    {
      "outfit_id": "guide_1233439055010869248",
      "outfit_rank": 0,
      "items": [
        {
          "sku_id": "T11W623603FLK",
          "spu_id": "T11W623603F",
          "id_goods": "676029",
          "role": "bottoms",
          "title": "女士宽松针织短裤",
          "price": 540.0,
          "sku_image_url": "https://img.fishfay.com/shopgoods/7/T11W623603F/T11W623603FLK/11/62804ab081e9deb8b163c5c2209d5844.jpg"
        },
        {
          "sku_id": "F11W621101FWT",
          "spu_id": "F11W621101F",
          "id_goods": "676030",
          "role": "top",
          "title": "【热销款】【菁英POLO】女士基础短袖POLO",
          "price": 580.0,
          "sku_image_url": "https://img.fishfay.com/shopgoods/7/F11W621101F/F11W621101FWT/11/61b133e2da621434d2b09b5055b8066d.jpg"
        }
      ],
      "outfit_tryon_image": "https://img.fishfay.com/tryon/outfit_123.jpg",
      "reason": "这套搭配巧妙结合了通勤与舒适，修身长袖T恤勾勒利落线条，搭配舒适梭织长裤，让每一步都自在无拘束……"
    }
  ]
}
```

### 1.5 错误码

均按上述「错误响应统一格式」返回 `code` / `message` / `trace_id`。

| HTTP | 触发条件 | `message` 示例 |
| --- | --- | --- |
| 400 | `app_id` 为空；或 `input_sku_id`/`image_url`/`message` 全空；或 `input_sku_id` 格式非法 | `app_id required` / `at least one of input_sku_id/image_url/message required` / `invalid sku_id format` |
| 401 | `app_id` 不在白名单；或缺 `X-API-Key`；或 Key 无效/停用/过期；或 `app_id` 与 Key 绑定不匹配 | `invalid app_id` / `API key required` / `invalid API key` / `API key expired` / `app_id mismatch with API key` |
| 403 | API Key 无权访问该接口（`allowed_apis` 不含） | `access denied: api not allowed` |
| 422 | `app_id` 缺失；或 `tryon` 非 bool（含 `1`/`"true"` 等） | `app_id: Field required` / `tryon: Input should be a valid boolean` |
| 429 | 超过 QPM / 日调用量；或排队队列已满 / 排队超时 | `rate limit exceeded: 100 req/min` / `daily limit exceeded` / `queue full, try again later` / `queue timeout after 30s` |
| 5xx | 服务异常（ES/Milvus/LLM/embedding 等未捕获故障） | `RuntimeError: ES connection refused: timeout 30s` |

> 鉴权开启后，排队请求的响应额外带 `X-Queue-Status`（immediate/queued）/ `X-Queue-Wait`（秒）/ `X-Queue-Position` 头。

> `image_url` 抓取失败已做降级（仅改用 `input_sku_id` 锚点），**不会因此 500**；5xx 均为真实内部故障，`message` 含异常类型与原因，可凭 `trace_id` 查服务端日志。

---

## 2. 重新生成推荐理由接口

### `POST /v1/outfit/regenerate-reason`

根据 `outfit_id` 重新生成一套搭配的推荐理由（约 300 字）。

### 2.1 请求参数

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `outfit_id` | string | 是 | 要重新生成理由的搭配 id |
| `reason_style` | string | 否 | 话术风格（透传暂不生效） |

### 2.2 响应

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `outfit_id` | string | 搭配 id |
| `reason` | string | 重新生成的推荐理由 |
| `trace_id` | string | 服务端生成的 `uuid4().hex`，与错误 envelope 同源；凭此查服务端日志联调 |

```json
{
  "outfit_id": "-255050021",
  "reason": "这套搭配专为轻松惬意的周末出游或日常通勤设计，修身长袖T恤勾勒利落线条……",
  "trace_id": "4767d73c23a147b4bfd677106727f12c"
}
```

### 2.3 错误码

均按「错误响应统一格式」返回 `code` / `message` / `trace_id`。

| HTTP | 触发条件 | `message` 示例 |
| --- | --- | --- |
| 422 | `outfit_id` 缺失或为空串 | `outfit_id: Field required` / `outfit_id: String should have at least 1 character` |
| 404 | outfit 不存在（缓存与 ES 均未命中），或 outfit 无 items | `outfit not found` / `outfit has no items` |
| 5xx | ES 兜底重建或 LLM 生成理由时内部故障 | `ConnectionError: ...`（凭 `trace_id` 查日志） |

> 解析顺序：先查进程内推荐缓存（同 worker 命中最佳）；未命中则回退 ES outfits 索引重建卡片再调 LLM 生成理由。**ES 兜底或 LLM 阶段的内部故障不再伪装成 404**，直接返回 5xx + `trace_id`，便于区分"没找到"与"服务故障"。

---

## 3. curl 测试用例

> 以下 `$HOST` 默认 `10.235.104.32:31869`，`jq` 用于美化输出（可选）。
> 鉴权开启后所有示例需加 `-H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01'`（见下示例）。

### 3.1 搭配推荐 —— 锚点 SKU

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{
    "app_id": "micro_guide",
    "input_sku_id": "F11W619219FPK",
    "message": "日常通勤",
    "tryon": false
  }' --max-time 90 | jq
```

### 3.2 搭配推荐 —— 带试穿

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{
    "app_id": "micro_guide",
    "input_sku_id": "F11W619219FPK",
    "message": "周末出游",
    "tryon": true
  }' --max-time 120 | jq '.outfits[0] | {outfit_id, outfit_rank, outfit_tryon_image, reason}'
```

### 3.3 搭配推荐 —— 仅图无 sku（图作为锚点）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{
    "app_id": "micro_guide",
    "image_url": "https://img.fishfay.com/shopgoods/7/F11W621101F/F11W621101FWT/11/61b133e2da621434d2b09b5055b8066d.jpg"
  }' --max-time 120 | jq
# 断言：含 img_ 锚点 item 的 sku_image_url == 入参 image_url；其余 item 为真实 URL
```

### 3.4 搭配推荐 —— 指定 session_id

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{
    "session_id": "a1b2c3d4e5f6789012345678abcdef01",
    "app_id": "micro_guide",
    "input_sku_id": "F11W619219FPK"
  }' --max-time 90 | jq '{session_id, input_sku_id, outfit_count: (.outfits|length)}'
```

### 3.5 校验 —— 缺 app_id（422）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"input_sku_id": "F11W619219FPK"}' | jq
# {"code": 422, "message": "app_id: Field required", "trace_id": "..."}
```

### 3.6 校验 —— app_id 空或无 sku/image/message（400）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"app_id": "micro_guide"}' | jq
# {"code": 400, "message": "at least one of input_sku_id/image_url/message required", "trace_id": "..."}

curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"app_id": ""}' | jq
# {"code": 400, "message": "app_id required", "trace_id": "..."}
```

### 3.7 重新生成理由 —— 已知 outfit_id（200）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/regenerate-reason \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"outfit_id": "-255050021"}' --max-time 60 | jq
# {"outfit_id": "-255050021", "reason": "这套搭配专为……"}
```

### 3.8 重新生成理由 —— 未知 outfit_id（404）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/regenerate-reason \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"outfit_id": "does_not_exist_xyz"}' | jq
# {"code": 404, "message": "outfit not found", "trace_id": "..."}
```

### 3.9 重新生成理由 —— 缺 outfit_id（422）

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/regenerate-reason \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{}' | jq
# {"code": 422, "message": "outfit_id: Field required", "trace_id": "..."}
```

### 3.10 辅助：取一个真实 outfit_id / sku_id 做测试

```bash
# 取 outfits 索引里的 outfit_id 及其 item sku_id
curl -s "http://10.235.104.32:31869/api/outfits?size=1" | \
  jq '.outfits[0] | {outfit_id, items: [.items[] | {sku_id, role}][:3]}'

# 取某 SKU 详情（确认 id_goods / tryon_image 存在）
curl -s "http://10.235.104.32:31869/skus/F11W619219FPK" | jq
```

---

## 4. 字段映射关系（实现说明）

| 文档出参字段 | 来源 |
| --- | --- |
| `session_id` | 入参 `session_id` 或服务端生成 |
| `input_sku_id` | 透传入参 |
| `outfit_id` | 引擎 outfit card |
| `outfit_rank` | 列表下标 0..N |
| `items[].sku_id/spu_id/role/title/price` | outfit card item |
| `items[].id_goods` | ES `skus` 索引按 sku_id 批量回填 |
| `items[].sku_image_url` | item `tryon_image`（被上传图覆盖回退 ES 真实图；`img_` 锚点取入参 `image_url`） |
| `outfit_tryon_image` | outfit card `outfit_tryon_image` |
| `reason` | LLM 生成（`skip_reason=false` 强制生成） |

底层引擎复用 `RecommendService.chat_stream`（意图→多路召回→排序→推荐理由→虚拟试穿全流程），对外接口仅做入参映射与出参整形，不影响既有 `/recommend/outfits`、`/regenerate-reason`、`/chat` 及调试 HTML。
