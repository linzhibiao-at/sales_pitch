# FILA 穿搭推荐接口鉴权方案

## 一、背景与问题

### 1.1 现状

边界测试发现接口存在两个安全缺陷：

| 问题编号 | 严重程度 | 描述 |
|---------|---------|------|
| ISS-05 | 🔴 高 | 所有接口（recommend / regenerate-reason / GET outfits / GET sku）完全无鉴权，任何人能访问服务地址即可调用 |
| ISS-04 | ⚠️ 中 | `app_id` 只校验是否为空，不校验值，`test`/`12345`/`@#$%` 等任意字符串均可通过 |

### 1.2 风险

1. **资源滥用**：recommend 接口每次调用耗时 3-5s 并消耗 LLM 资源，无鉴权可被恶意刷量
2. **数据爬取**：GET 接口可无限制查询 outfits 列表和 SKU 详情
3. **无法追责**：无调用方标识，无法做应用级限流和计费
4. **无法灰度**：无法区分调用方做灰度发布或功能开关

### 1.3 需鉴权的接口

| 接口 | 方法 | 说明 | 资源消耗 |
|------|------|------|---------|
| `/v1/outfit/recommend` | POST | 搭配推荐 | 高（LLM + ES + Milvus） |
| `/v1/outfit/regenerate-reason` | POST | 重生成理由 | 中（LLM） |
| `/api/outfits` | GET | outfits 列表 | 低（ES） |
| `/skus/{sku_id}` | GET | SKU 详情 | 低（ES） |

---

## 二、方案目标

1. **身份认证**：识别调用方身份，拒绝匿名访问
2. **权限控制**：不同调用方可配置不同的接口访问权限
3. **限流防护**：按调用方维度限流，防止资源滥用
4. **可追溯**：所有请求可关联到调用方，便于审计排查
5. **低改造成本**：对现有调用方改动最小，平滑迁移

---

## 三、鉴权方案选型

### 3.1 方案对比

| 方案 | 复杂度 | 适用场景 | 是否选用 |
|------|--------|---------|---------|
| **API Key** | 低 | B2B 内部接口、调用方数量有限 | ✅ 选用 |
| JWT | 中 | 多服务 SSO、用户级认证 | ❌ 过重 |
| OAuth2 | 高 | 第三方开放平台、用户授权 | ❌ 过重 |
| HMAC 签名 | 中 | 高安全要求、防重放 | ⏳ 二期可选 |

### 3.2 选型理由

本接口为 B2B 对外接口，调用方数量有限（小程序、导购 App 等），无需用户级认证。**API Key 方案**最简单实用：

- 调用方只需在请求头携带 `X-API-Key`
- 服务端校验 Key 有效性即可
- 无需 Token 刷新、过期等复杂逻辑
- 后续如需更高安全可叠加 HMAC 签名

---

## 四、详细设计

### 4.1 认证流程

```
调用方                          服务端
  |                               |
  |  POST /v1/outfit/recommend    |
  |  Header: X-API-Key: xxx       |
  |  Body: {...}                  |
  |------------------------------>|
  |                               |
  |                  1. 提取 X-API-Key
  |                  2. 查 Key 白名单（内存/Redis）
  |                  3. 校验 app_id 是否匹配 Key 绑定的 app_id
  |                  4. 校验限流（按 app_id 维度）
  |                  5. 执行业务逻辑
  |                               |
  |  200 {outfits: [...]}         |
  |<------------------------------|
  |                               |
```

### 4.2 请求格式

调用方需在 HTTP 请求头中携带：

```
X-API-Key: ak_<32位随机字符串>
```

**请求示例：**

```bash
curl -s -XPOST http://10.235.104.32:31869/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{
    "app_id": "micro_guide",
    "input_sku_id": "A11M627701FPK",
    "tryon": false
  }'
```

### 4.3 API Key 与 app_id 的关系

```
┌─────────────────────────────────────────────┐
│              API Key (X-API-Key)             │
│  ak_a1b2c3d4e5f6789012345678abcdef01        │
│                                              │
│  绑定关系：                                   │
│  ├── app_id: micro_guide    （导购小程序）    │
│  ├── name: 导购小程序                        │
│  ├── allowed_apis: [recommend, regenerate]  │
│  ├── rate_limit: 100 QPM                    │
│  ├── daily_limit: 10000 次/天               │
│  ├── status: active                         │
│  └── created_at: 2026-07-23                 │
└─────────────────────────────────────────────┘
```

**规则：**
- 一个 API Key 绑定一个 `app_id`，请求体中的 `app_id` 必须与 Key 绑定的 `app_id` 一致
- `app_id` 不一致返回 401 `app_id mismatch with API Key`
- 一个 `app_id` 可签发多个 API Key（便于轮换）

### 4.4 错误响应

鉴权失败统一返回 `code` / `message` / `trace_id` 格式，与现有错误响应一致：

| HTTP | code | 触发条件 | message 示例 |
|------|------|---------|-------------|
| 401 | 401 | 缺少 `X-API-Key` 请求头 | `API key required` |
| 401 | 401 | API Key 无效或已停用 | `invalid API key` |
| 401 | 401 | `app_id` 与 Key 绑定的不匹配 | `app_id mismatch with API key` |
| 403 | 403 | API Key 无权访问该接口 | `access denied: api not allowed` |
| 429 | 429 | 超过 QPM / 日调用量限制 | `rate limit exceeded: {limit} req/min` |
| 429 | 429 | 排队队列已满 | `queue full, try again later` |
| 429 | 429 | 排队等待超时 | `queue timeout after {n}s` |

**响应示例：**

```json
{
  "code": 401,
  "message": "invalid API key",
  "trace_id": "4767d73c23a147b4bfd677106727f12c"
}
```

```json
{
  "code": 429,
  "message": "rate limit exceeded: 100 req/min",
  "trace_id": "4767d73c23a147b4bfd677106727f12c"
}
```

```json
{
  "code": 429,
  "message": "queue timeout after 30s",
  "trace_id": "4767d73c23a147b4bfd677106727f12c"
}
```

---

## 五、限流与排队设计

### 5.1 设计思路

超过流量限制时**不直接拒绝**，而是进入**排队等待**机制，提升用户体验：

```
请求到达
  │
  ├─ QPM / 日调用量检查 ──超出──▶ 429 直接拒绝（硬限制）
  │
  └─ 并发数检查
       │
       ├─ 未满 ──▶ 立即执行
       │
       └─ 已满 ──▶ 进入排队队列
                    │
                    ├─ 排队等待 ≤ 超时时间 ──▶ 获取到并发槽 ──▶ 执行
                    │
                    ├─ 排队超时 ──▶ 429 queue timeout
                    │
                    └─ 队列已满 ──▶ 429 queue full
```

**两级限流策略：**

| 限制类型 | 超限行为 | 说明 |
|---------|---------|------|
| QPM（每分钟请求数） | 直接拒绝 429 | 硬限制，防恶意刷量 |
| 日调用量 | 直接拒绝 429 | 硬限制，防超额消耗 |
| 并发数 | **排队等待** | 软限制，平滑处理突发流量 |

### 5.2 排队机制详解

#### 5.2.1 工作流程

```
                    ┌─────────────────────────────────────┐
                    │        并发执行槽 (concurrent=10)     │
                    │  [req1] [req2] [req3] ... [req10]    │
                    └──────────────┬──────────────────────┘
                                   │ 满了
                    ┌──────────────▼──────────────────────┐
                    │        等待队列 (queue_size=20)      │
                    │  [req11] → [req12] → [req13] → ...  │
                    └──────────────┬──────────────────────┘
                                   │ 队列也满了
                    ┌──────────────▼──────────────────────┐
                    │         429 queue full               │
                    └─────────────────────────────────────┘
```

1. 请求到达，检查并发槽是否有空闲
2. 有空闲 → 直接获取槽位，执行请求
3. 无空闲 → 进入等待队列排队
4. 排队中持续等待，直到有槽位释放或超时
5. 槽位释放 → 队首请求获取槽位，开始执行
6. 排队超时 → 返回 429 `queue timeout`
7. 队列已满 → 返回 429 `queue full`

#### 5.2.2 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `concurrent` | 10 | 最大并发执行数（同时处理的请求数） |
| `queue_size` | 20 | 等待队列最大长度（排队中的请求数） |
| `queue_timeout` | 30s | 排队最长等待时间，超时返回 429 |
| `qpm` | 100 | 每分钟最大请求数（含排队和执行） |
| `daily` | 10000 | 每日最大调用量 |

#### 5.2.3 排队响应头

排队请求的响应中增加以下头，便于调用方感知排队情况：

```
X-Queue-Status: queued        # 是否排队：immediate / queued
X-Queue-Wait: 2.3             # 实际排队等待时间（秒）
X-Queue-Position: 5           # 入队时排队位置（第几位）
```

### 5.3 限流配置

不同 app_id 可配置不同阈值，在 `config.yaml` 中管理：

```yaml
auth:
  enabled: true
  rate_limit:
    default_qpm: 100
    default_daily: 10000
    default_concurrent: 5
    default_queue_size: 20
    default_queue_timeout: 30
  keys:
    - api_key: "ak_a1b2c3d4e5f6789012345678abcdef01"
      app_id: "micro_guide"
      name: "导购小程序"
      allowed_apis: ["recommend", "regenerate-reason"]
      rate_limit:
        qpm: 200
        daily: 50000
        concurrent: 10
        queue_size: 30
        queue_timeout: 30
      status: active
    - api_key: "ak_f1e2d3c4b5a69788697564534323120f"
      app_id: "wechat_mini"
      name: "微信小程序"
      allowed_apis: ["recommend"]
      rate_limit:
        qpm: 50
        daily: 5000
        concurrent: 3
        queue_size: 10
        queue_timeout: 15
      status: active
```

### 5.4 限流实现

#### 5.4.1 QPM / 日限流（硬限制）

使用 Redis 计数器，超限直接拒绝：

- **QPM 限流**：Redis `INCR` + `EXPIRE 60`，key 为 `rl:{app_id}:min:{yyyymmddHHMM}`
- **日限流**：Redis `INCR` + `EXPIRE 86400`，key 为 `rl:{app_id}:day:{yyyymmdd}`

#### 5.4.2 并发 + 排队（软限制）

使用 Redis + asyncio 实现：

```python
import asyncio
import time
from fastapi import HTTPException

class ConcurrencyQueue:
    """并发槽 + 排队队列管理器"""

    def __init__(self, concurrent: int, queue_size: int, queue_timeout: int):
        self.concurrent = concurrent          # 最大并发数
        self.queue_size = queue_size           # 队列容量
        self.queue_timeout = queue_timeout     # 排队超时(秒)
        self._semaphore = asyncio.Semaphore(concurrent)
        self._queue_waiting = 0                # 当前排队等待数

    async def acquire(self) -> dict:
        """获取执行槽位，返回排队信息"""
        queue_status = "immediate"
        queue_wait = 0.0
        queue_position = 0

        # 检查队列是否已满
        if self._queue_waiting >= self.queue_size:
            raise HTTPException(
                status_code=429,
                detail="queue full, try again later"
            )

        self._queue_waiting += 1
        queue_position = self._queue_waiting

        # 尝试获取信号量（有并发槽则立即，无则排队等待）
        if self._semaphore.locked():
            queue_status = "queued"

        start_time = time.time()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.queue_timeout
            )
            queue_wait = round(time.time() - start_time, 3)
        except asyncio.TimeoutError:
            self._queue_waiting -= 1
            raise HTTPException(
                status_code=429,
                detail=f"queue timeout after {self.queue_timeout}s"
            )

        self._queue_waiting -= 1
        return {
            "queue_status": queue_status,
            "queue_wait": queue_wait,
            "queue_position": queue_position,
        }

    def release(self):
        """释放执行槽位"""
        self._semaphore.release()
```

#### 5.4.3 中间件集成

```python
async def verify_api_key(request: Request, ...):
    # ... 鉴权逻辑 ...

    # QPM / 日限流检查（硬限制）
    await rate_limiter.check_quota(app_id, key_info["rate_limit"])

    # 并发 + 排队（软限制）
    queue_info = await concurrency_queue.acquire(
        concurrent=key_info["rate_limit"]["concurrent"],
        queue_size=key_info["rate_limit"]["queue_size"],
        queue_timeout=key_info["rate_limit"]["queue_timeout"],
    )

    # 排队信息注入响应头
    request.state.queue_info = queue_info

    try:
        yield  # 执行业务逻辑
    finally:
        concurrency_queue.release()  # 释放槽位


# 响应中间件：注入排队头
@app.middleware("http")
async def add_queue_headers(request: Request, call_next):
    response = await call_next(request)
    if hasattr(request.state, "queue_info"):
        info = request.state.queue_info
        response.headers["X-Queue-Status"] = info["queue_status"]
        response.headers["X-Queue-Wait"] = str(info["queue_wait"])
        response.headers["X-Queue-Position"] = str(info["queue_position"])
    return response
```

### 5.5 无 Redis 降级

- **Redis 不可用时**：QPM / 日限流跳过（仅记录告警日志），并发排队退化为**进程内 asyncio 信号量**（单 worker 有效，多 worker 下并发数可能略超）
- **保证可用性优先**：限流降级不影响服务可用性

### 5.6 排队场景示例

**场景**：导购小程序搞活动，瞬时 50 个请求涌入，配置 `concurrent=10, queue_size=30, queue_timeout=30s`。

| 时间 | 到达请求 | 并发槽 | 队列 | 行为 |
|------|---------|--------|------|------|
| t=0 | 50 个请求 | 10 个占用 | 30 个排队 | 10 个立即执行，30 个排队，**10 个返回 429 queue full** |
| t=3s | 第 1 个完成 | 9 个占用 | 29 个排队 | 队首 1 个进入执行 |
| t=5s | 又来 5 个 | 10 个占用 | 队列满 | 5 个返回 429 queue full |
| t=30s | 队列中 10 个等了 30s | - | - | **10 个返回 429 queue timeout** |
| t=35s | 大部分请求处理完 | 3 个占用 | 0 | 新请求立即执行 |

---

## 六、配置管理

### 6.1 API Key 管理

API Key 存储在 `config.yaml`，支持热加载（文件变更自动重载）：

```yaml
auth:
  enabled: true                # 鉴权总开关，false 则跳过鉴权（调试用）
  header_name: "X-API-Key"     # 请求头字段名
  keys_file: "config/api_keys.yaml"  # 独立 Key 文件（可选）
```

`config/api_keys.yaml`：

```yaml
keys:
  - api_key: "ak_a1b2c3d4e5f6789012345678abcdef01"
    app_id: "micro_guide"
    name: "导购小程序"
    allowed_apis: ["recommend", "regenerate-reason"]
    rate_limit:
      qpm: 200
      daily: 50000
      concurrent: 10
      queue_size: 30
      queue_timeout: 30
    status: active
    created_at: "2026-07-23"
    expires_at: null            # null 表示永不过期

  - api_key: "ak_f1e2d3c4b5a69788697564534323120f"
    app_id: "wechat_mini"
    name: "微信小程序"
    allowed_apis: ["recommend"]
    rate_limit:
      qpm: 50
      daily: 5000
      concurrent: 3
      queue_size: 10
      queue_timeout: 15
    status: active
    created_at: "2026-07-23"
    expires_at: "2027-07-23"
```

### 6.2 Key 生成规则

```
ak_<32位小写十六进制>
```

生成命令：

```bash
python3 -c "import secrets; print('ak_' + secrets.token_hex(16))"
# 示例：ak_a1b2c3d4e5f6789012345678abcdef01
```

### 6.3 Key 生命周期

| 操作 | 方式 | 说明 |
|------|------|------|
| 签发 | 编辑 `api_keys.yaml` 添加条目 | 热加载生效 |
| 停用 | `status: inactive` | 立即拒绝该 Key |
| 轮换 | 新增 Key + 旧 Key 设 inactive | 建议双 Key 并存 7 天过渡 |
| 过期 | `expires_at: "2027-07-23"` | 到期自动失效 |

---

## 七、接口改造说明

### 7.1 新增鉴权中间件

在 FastAPI 中添加鉴权依赖，所有需鉴权的接口注入：

```python
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader

# 请求头 Key 定义
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(
    request: Request,
    api_key: str = Depends(api_key_header),
):
    """鉴权中间件：校验 API Key + app_id + 限流"""
    # 1. 检查鉴权开关
    if not config.auth.enabled:
        return  # 调试模式跳过

    # 2. 校验 Key 存在
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    # 3. 查 Key 白名单
    key_info = key_store.get(api_key)
    if not key_info or key_info["status"] != "active":
        raise HTTPException(status_code=401, detail="invalid API key")

    # 4. 校验过期
    if key_info.get("expires_at") and datetime.now() > key_info["expires_at"]:
        raise HTTPException(status_code=401, detail="API key expired")

    # 5. 校验 app_id 匹配（从 body 提取）
    body = await request.json()
    app_id = body.get("app_id", "")
    if app_id != key_info["app_id"]:
        raise HTTPException(status_code=401, detail="app_id mismatch with API key")

    # 6. 校验接口权限
    api_name = route_to_api_name(request.url.path)
    if api_name not in key_info["allowed_apis"]:
        raise HTTPException(status_code=403, detail="access denied: api not allowed")

    # 7. 限流检查
    await rate_limiter.check(app_id, key_info["rate_limit"])

    # 8. 注入调用方信息到 request.state
    request.state.caller = key_info
```

### 7.2 路由改造

```python
@app.post("/v1/outfit/recommend")
async def recommend(
    request: Request,
    payload: RecommendRequest,
    _auth=Depends(verify_api_key),   # 新增鉴权依赖
):
    ...

@app.post("/v1/outfit/regenerate-reason")
async def regenerate_reason(
    request: Request,
    payload: RegenerateRequest,
    _auth=Depends(verify_api_key),   # 新增鉴权依赖
):
    ...
```

### 7.3 错误响应适配

鉴权错误需适配现有 `{code, message, trace_id}` 格式：

```python
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    trace_id = getattr(request.state, "trace_id", uuid4().hex)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": exc.detail,
            "trace_id": trace_id,
        },
    )
```

### 7.4 GET 接口改造

GET 接口（`/api/outfits`、`/skus/{sku_id}`）无 `app_id` 入参，改为仅校验 API Key 有效性：

```python
async def verify_api_key_get(
    request: Request,
    api_key: str = Depends(api_key_header),
):
    """GET 接口鉴权：仅校验 Key 有效性 + 接口权限"""
    if not config.auth.enabled:
        return
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    key_info = key_store.get(api_key)
    if not key_info or key_info["status"] != "active":
        raise HTTPException(status_code=401, detail="invalid API key")
    # GET 接口默认允许所有有效 Key
    request.state.caller = key_info
```

---

## 八、调用方迁移指南

### 8.1 迁移步骤

1. **服务端签发 API Key**：为每个调用方生成 Key 并配置到 `api_keys.yaml`
2. **通知调用方**：提供 API Key、文档、迁移截止日期
3. **双模式过渡期（7 天）**：`auth.enabled=true` 但记录无 Key 请求日志，不拦截
4. **强制鉴权**：过渡期结束后 `auth.enabled=true` 严格拦截无 Key 请求

### 8.2 调用方改动

调用方仅需在请求头增加 `X-API-Key`，请求体不变：

**改造前：**
```bash
curl -XPOST http://host:port/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"micro_guide","input_sku_id":"A11M627701FPK"}'
```

**改造后：**
```bash
curl -XPOST http://host:port/v1/outfit/recommend \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"app_id":"micro_guide","input_sku_id":"A11M627701FPK"}'
```

### 8.3 错误处理建议

调用方需处理以下新增错误码：

| code | 处理建议 |
|------|---------|
| 401 | 检查 API Key 是否正确、是否过期，联系管理员重新签发 |
| 403 | 当前 Key 无权访问该接口，联系管理员开通权限 |
| 429 `rate limit exceeded` | QPM/日限流触发，客户端退避重试（建议指数退避：1s → 2s → 4s） |
| 429 `queue full` | 排队队列已满，稍后重试 |
| 429 `queue timeout` | 排队超时未获取到执行槽，稍后重试或降低请求频率 |

---

## 九、测试验证

### 9.1 鉴权功能测试用例

| 编号 | 场景 | 预期结果 |
|------|------|---------|
| AUTH-01 | 无 `X-API-Key` 头 | 401 `API key required` |
| AUTH-02 | 错误的 API Key | 401 `invalid API key` |
| AUTH-03 | 正确 API Key + 匹配 app_id | 200 正常返回 |
| AUTH-04 | 正确 API Key + 不匹配 app_id | 401 `app_id mismatch` |
| AUTH-05 | 已停用的 Key | 401 `invalid API key` |
| AUTH-06 | 已过期的 Key | 401 `API key expired` |
| AUTH-07 | Key 无权访问的接口 | 403 `access denied` |
| AUTH-08 | `auth.enabled=false` 时无 Key | 200 正常返回（调试模式） |

### 9.2 限流与排队测试用例

| 编号 | 场景 | 预期结果 |
|------|------|---------|
| RATE-01 | QPM 内正常请求 | 200，响应头 `X-Queue-Status: immediate` |
| RATE-02 | 超过 QPM 阈值 | 429 `rate limit exceeded` |
| RATE-03 | 超过日调用量 | 429 `daily limit exceeded` |
| RATE-04 | 并发未满时请求 | 200，`X-Queue-Status: immediate`，`X-Queue-Wait: 0` |
| RATE-05 | 并发已满，进入排队 | 200，`X-Queue-Status: queued`，`X-Queue-Wait > 0` |
| RATE-06 | 排队等待超时 | 429 `queue timeout after {n}s` |
| RATE-07 | 排队队列已满 | 429 `queue full, try again later` |
| RATE-08 | 排队期间前序请求完成 | 200，`X-Queue-Status: queued` |
| RATE-09 | QPM 窗口过后恢复 | 200 |

### 9.3 验证脚本

```bash
# AUTH-01: 无 Key
curl -s -w '\nHTTP:%{http_code}' -XPOST "$HOST/v1/outfit/recommend" \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"micro_guide","input_sku_id":"A11M627701FPK"}'
# 预期: 401 "API key required"

# AUTH-03: 正确 Key
curl -s -w '\nHTTP:%{http_code}' -XPOST "$HOST/v1/outfit/recommend" \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"app_id":"micro_guide","input_sku_id":"A11M627701FPK"}'
# 预期: 200

# AUTH-04: app_id 不匹配
curl -s -w '\nHTTP:%{http_code}' -XPOST "$HOST/v1/outfit/recommend" \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: ak_a1b2c3d4e5f6789012345678abcdef01' \
  -d '{"app_id":"wrong_app","input_sku_id":"A11M627701FPK"}'
# 预期: 401 "app_id mismatch"
```

---

## 十、实施计划

### 10.1 阶段划分

| 阶段 | 内容 | 说明 |
|------|------|------|
| **阶段一** | 基础鉴权 | API Key 校验 + app_id 绑定校验，解决 ISS-04/ISS-05 |
| **阶段二** | 接口权限 | 按 API Key 配置 allowed_apis，精细控制接口访问 |
| **阶段三** | 限流与排队 | QPM + 日调用量（硬限制）+ 并发排队（软限制） |
| **阶段四** | 安全增强（可选） | HMAC 签名防重放、Key 自动轮换、审计日志 |

### 10.2 落地步骤

1. **新建 `config/api_keys.yaml`**，定义 Key 白名单结构
2. **实现 `ApiKeyStore`**：加载 Key 配置，支持热加载
3. **实现 `verify_api_key` 中间件**：Key 校验 + app_id 匹配
4. **实现 `RateLimiter`**：基于 Redis 的 QPM/日限流（硬限制）
5. **实现 `ConcurrencyQueue`**：基于 asyncio 信号量的并发排队（软限制）
6. **路由注入鉴权依赖**：4 个接口添加 `Depends(verify_api_key)`
7. **响应头注入**：排队信息通过 `X-Queue-*` 响应头返回
8. **错误响应适配**：401/403/429 返回 `{code, message, trace_id}`
9. **`config.yaml` 增加 `auth` 配置段**
10. **为现有调用方签发 API Key**
11. **双模式过渡期**：7 天观察期，记录无 Key 请求但不拦截
12. **强制开启**：`auth.enabled=true`

### 10.3 回滚方案

- `auth.enabled=false` 可立即关闭鉴权，回到当前无鉴权状态
- API Key 配置文件独立，删除文件即回滚
- 限流依赖 Redis，Redis 不可用时自动降级为不限流

---

## 十一、配置文件结构汇总

### `config.yaml` 新增段

```yaml
auth:
  enabled: false                # 鉴权总开关（上线时改 true）
  header_name: "X-API-Key"
  keys_file: "config/api_keys.yaml"
  rate_limit:
    default_qpm: 100
    default_daily: 10000
    default_concurrent: 5
    default_queue_size: 20         # 排队队列容量
    default_queue_timeout: 30      # 排队超时(秒)
    redis_url: "redis://localhost:6379/0"  # 限流用 Redis
    fallback_no_redis: true                 # Redis 不可用时是否放行
```

### `config/api_keys.yaml`

```yaml
keys:
  - api_key: "ak_a1b2c3d4e5f6789012345678abcdef01"
    app_id: "micro_guide"
    name: "导购小程序"
    allowed_apis: ["recommend", "regenerate-reason", "get_outfits", "get_sku"]
    rate_limit:
      qpm: 200
      daily: 50000
      concurrent: 10
      queue_size: 30
      queue_timeout: 30
    status: active
    created_at: "2026-07-23"
    expires_at: null
```

---

## 十二、安全建议

1. **API Key 不入 Git**：`api_keys.yaml` 加入 `.gitignore`，通过运维平台或配置中心管理
2. **HTTPS 部署**：API Key 在请求头明文传输，生产环境必须 HTTPS
3. **Key 定期轮换**：建议每 6 个月轮换一次，双 Key 过渡 7 天
4. **日志脱敏**：日志中 API Key 脱敏显示（仅前 8 位 + `***`）
5. **监控告警**：401 错误率突增告警（可能是 Key 泄露或攻击）
6. **审计日志**：记录每次请求的 `app_id` + `trace_id` + 接口 + 耗时，便于追责
