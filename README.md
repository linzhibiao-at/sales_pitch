# FILA 营销话术服务（`sales_pitch`）

基于大模型的导购营销话术生成服务：接收**顾客画像 + 商品信息**，调用 LLM 生成面向不同渠道/风格的导购话术，以 HTTP 接口对外提供。工程由 FILA 穿搭推荐工程裁剪而来，仅保留话术生成链路；启动与部署方式沿用原方案（uvicorn + Docker + K8s）。

***

## 1. 能力概览

| 能力 | 接口 | 说明 |
| --- | --- | --- |
| 话术生成 | `POST /v1/sales-pitch/generate` | 顾客信息 + 商品清单 → 导购话术（支持风格/渠道/字数要求） |
| 健康检查 | `GET /health` | 探活（K8s liveness/readiness） |
| 审计列表 | `GET /api/audit/requests` | 按 trace_id/app_id/时间等过滤请求审计 |
| 审计详情 | `GET /api/audit/requests/{trace_id}` | 单条审计完整文档（输入/结果） |

***

## 2. 目录结构

```
sales_pitch/
├── backend/
│   ├── main.py                 # FastAPI 路由（health / 话术 / 审计查询）
│   ├── auth.py                 # API Key 鉴权 + 进程内限流排队
│   ├── models.py               # Pydantic 入参模型（SalesPitch*）
│   ├── llm_client.py           # OpenAI 兼容 LLM 客户端（httpx 连接池 + 模型链降级）
│   ├── config.py               # config.yaml 加载（mtime 缓存）+ ES 客户端构造
│   ├── es_client.py            # Elasticsearch 可选客户端（审计落库/查询）
│   ├── prompt_loader.py        # prompt/*.md 加载（mtime 缓存 + 热重载）
│   ├── api_debug.py            # HTTP/LLM IO 调试日志
│   ├── logging_config.py       # 集中式日志格式化
│   └── services/
│       ├── sales_pitch_service.py   # 话术生成编排（prompt 拼装 → LLM → 审计）
│       └── request_audit.py         # 审计文档构造 + ES 写/查
├── prompt/
│   └── sales_pitch.md          # 话术提示词（FILA 金牌导购角色设定）
├── config/
│   └── api_keys.yaml           # API Key 白名单（不入 Git）
├── tests/                      # 单测（话术模型/服务 + 鉴权限流 + 审计）
├── docs/                       # 历史架构/鉴权限流文档（部署方案参考）
├── config.yaml                 # 主配置
├── Dockerfile                  # 容器镜像（python:3.12-slim）
├── deployment*.yaml            # K8s Deployment（dev/prod）
├── setup.sh / restart.sh       # 本地环境搭建 / 重启脚本
└── requirements.txt
```

***

## 3. 快速开始

### 3.1 环境准备

```bash
./setup.sh                 # uv venv + pip 依赖
source .venv/bin/activate
```

或手动：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 3.2 环境变量（密钥，勿提交仓库）

| 变量 | 用途 |
| --- | --- |
| `ANTA_LLM_API_KEY` | 话术生成 LLM（安踏私有网关 `ai.anta.com`） |
| `ES_HOSTS` | 覆盖 `config.yaml` 的 ES 地址（逗号分隔，可选） |
| `ES_USERNAME` / `ES_PASSWORD` | ES 审计集群认证（可选，审计不依赖时不配） |
| `FILA_AGENT_DEBUG_API_IO` | HTTP/LLM IO 调试日志开关（默认读 yaml） |
| `FILA_AGENT_PROMPT_HOT_RELOAD` | prompt 热重载（开发态） |

### 3.3 启动

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

启动后：

- 健康检查：<http://127.0.0.1:8080/health>
- 接口文档：<http://127.0.0.1:8080/docs>（FastAPI 自动生成）

***

## 4. HTTP API

### 4.1 `POST /v1/sales-pitch/generate`

**请求头**：`X-API-Key: <key>`（`auth.enabled: true` 时必填）

**请求体**：

```json
{
  "session_id": "可选，幂等/联调追踪用",
  "app_id": "micro_guide",
  "customer": {
    "nickname": "王女士",
    "gender": "女",
    "age": "35",
    "style_preference": "简约通勤",
    "scene": "秋季通勤",
    "size_info": "M 码",
    "budget": "500-800元",
    "notes": "关注面料舒适度",
    "extra": {"会员等级": "金卡"}
  },
  "products": [
    {
      "sku_id": "F11W619219FPK",
      "title": "FILA 重磅纯棉连帽卫衣",
      "price": 599,
      "category": "卫衣",
      "color": "燕麦色",
      "material": "纯棉",
      "selling_points": "重磅面料；不变形；经典LOGO",
      "extra": {}
    }
  ],
  "pitch_style": "warm",
  "channel": "wechat",
  "max_length": 200
}
```

字段说明：

- `customer` 整体可选（缺省生成通用话术）；`extra` 为自由扩展字段，原样注入 prompt
- `products` 1~10 个；`title` 必填（清理 HTML 标签/lone surrogate 后须非空）
- `pitch_style`：`warm`（热情亲切）/ `professional`（专业顾问）/ `concise`（简短干练）或自由描述
- `channel`：`wechat` / `offline` / `phone` / `live` 或自由描述，影响排版与语气
- 入参自由文本自动剥除 HTML 标签（防 LLM 内容过滤）与非法 surrogate 码点

**响应**：

```json
{
  "session_id": "3f2b...",
  "pitch": "王女士您好！...",
  "pitch_style": "warm",
  "model": "qwen3.5-flash",
  "trace_id": "a1b2c3..."
}
```

**错误**（统一 envelope `{"code", "message", "trace_id"}`）：

| 状态 | 场景 |
| --- | --- |
| 400 | `app_id` 缺失 |
| 401 | 无 Key / Key 无效 / app_id 不在白名单 / app_id 与 Key 绑定不一致 |
| 403 | Key 未授权 `sales_pitch` 接口 |
| 422 | 入参校验失败 |
| 429 | QPM/日量超限、排队队列满或超时（响应头带 `X-Queue-*`） |
| 503 | LLM 空输出或上游故障 |

### 4.2 `GET /health`

```json
{"status": "ok", "service": "sales_pitch"}
```

### 4.3 `GET /api/audit/requests`

Query 参数：`trace_id` / `app_id` / `session_id` / `request_kind` / `status` / `ts_from` / `ts_to` / `size`（≤200）/ `offset`。审计关闭或 ES 不可用时返回 `{"enabled": false, "items": []}`。

### 4.4 `GET /api/audit/requests/{trace_id}`

单条审计完整文档（入参原文 + 话术前 600 字）；审计不可用 503，未命中 404。

***

## 5. 鉴权与限流

按 `docs/FILA接口鉴权与限流方案.md` 实现：

- **Key 白名单**：`config/api_keys.yaml`（`auth.keys_file`），支持热加载；Key 绑定 `app_id` 与 `allowed_apis`
- **app_id 双重校验**：顶层 `allowed_app_ids` 白名单 + 请求体 `app_id` 须与 Key 绑定值一致
- **限流**：进程内实现（不引 Redis），per `app_id` 的 QPM / 日调用量硬限制 + 并发排队（`asyncio.Semaphore`，队列满/超时 429）；多 worker 下为单进程近似
- `auth.enabled: false` 或 `log_only: true` 时不拦截（灰度过渡用）

***

## 6. 代码链路

```text
POST /v1/sales-pitch/generate
  → backend/auth.py            # Key 校验 + allowed_apis + QPM/日量 + 并发排队
  → backend/main.py            # app_id 白名单 + Key 绑定校验 → SalesPitchService.generate()
  → sales_pitch_service.py     # 顾客/商品/要求 → 三个中文文本块
  → backend/llm_client.py      # prompt/sales_pitch.md 为 system，调 {base_url}/chat/completions
                               # qwen3.5-flash 主模型，重试耗尽切 fallback qwen3.7-plus
  → （finally）request_audit.py  # 输入 + 结果落 ES fila-requests 索引（失败静默）
```

- LLM 为同步阻塞调用，经 `asyncio.to_thread` 包装不卡事件循环；进程级 httpx 连接池复用
- 4xx（非 429）不可重试直接换模型；429/5xx/超时/网络错误按 `retry_delay_sec` 重试
- `temperature=0.7`（创意生成任务，高于抽取/排序类调用）

***

## 7. 配置说明（`config.yaml`）

| 区块 | 说明 |
| --- | --- |
| `allowed_app_ids` | 对外接口 app_id 白名单（顶层键；缺失=不强制） |
| `auth` | 鉴权开关、header 名、keys_file、限流默认值 |
| `prompt_files` | 话术提示词路径（`prompt/sales_pitch.md`） |
| `models.sales_pitch_llm` | LLM 网关地址、密钥环境变量名、模型、超时、重试、fallback |
| `elasticsearch` | 审计集群地址（`ES_HOSTS` 可覆盖）、`requests` 索引、审计开关 |
| `logging` | 日志级别、`debug_api_io`、脱敏规则（`api_key`） |

修改 `config.yaml` / `api_keys.yaml` / `prompt/*.md` 均按 mtime 缓存热加载，无需重启。

***

## 8. 部署

### 8.1 Docker

```bash
docker build -t sales-pitch:latest .
docker run -p 8080:8080 \
  -e ANTA_LLM_API_KEY=... \
  -e ES_USERNAME=... -e ES_PASSWORD=... \
  sales-pitch:latest
```

镜像内 `CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4"]`。

### 8.2 Kubernetes

`deployment.yaml` / `deployment-dev.yaml` / `deployment-prod.yaml` 沿用原穿搭推荐工程的部署方案：

- `command: uvicorn backend.main:app --host 0.0.0.0 --port 8080 --workers 4`
- 健康探针 `GET /health`
- 秘钥经 env 注入（`ANTA_LLM_API_KEY`、`ES_USERNAME`、`ES_PASSWORD`）
- namespace：`f-aifit`（prod）/ `mgs-d`（dev）；资源 4CPU/8Gi

### 8.3 虚拟机/物理机

```bash
./restart.sh    # 停旧进程 → git pull → nohup uvicorn（日志 sales_pitch.log）
```

***

## 9. 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

| 文件 | 覆盖 |
| --- | --- |
| `test_sales_pitch.py` | 入参模型（清洗/校验）、文本块拼装、服务编排与审计（mock LLM） |
| `test_auth.py` | Key 白名单、路由映射、QPM/日量/并发排队、端到端鉴权接线 |
| `test_request_audit.py` | 审计开关、ES 客户端降级、审计文档构造与查询 |

***

## 10. 历史背景

本工程由 FILA 穿搭推荐工程（`fila_agent_html`）裁剪而来：删除了推荐召回/排序/意图/检索调试/评测/ETL 等业务模块，仅保留本次新增的营销话术生成链路与鉴权限流、审计、部署骨架。原工程的架构、鉴权限流、部署等方案文档保留在 `docs/` 供参考（如 `docs/FILA接口鉴权与限流方案.md`、`docs/系统架构文档.md`、`docs/deployment_architecture.drawio`）。
