---
last_updated: 2026-09-01
status: active          # active | deprecated | draft
owner: @linzhibiao
---

# 架构总览

## 模块结构

```
sales_pitch/
├── backend/                          # 应用代码（Python 3.12）
│   ├── main.py                       # FastAPI app 骨架 / 中间件 / 异常处理器 / 路由挂载
│   ├── models.py                     # Pydantic 入参模型 + 输入清理（HTML 标签 / lone surrogate）
│   ├── auth.py                       # API Key 鉴权 + 进程内限流排队（进程级单例）
│   ├── config.py                     # config.yaml 读取 + 环境变量覆盖（mtime 缓存）
│   ├── api_debug.py                  # HTTP/LLM 调试日志 + redact_for_log() 脱敏
│   ├── logging_config.py             # 集中式日志初始化（ReadableFormatter）
│   │
│   ├── routers/                      # 路由层（HTTP 协议边界）
│   │   ├── sales_pitch.py            #   POST /v1/sales-pitch/generate（模块级初始化 Agent 栈）
│   │   ├── audit.py                  #   GET  /v1/audit/requests[/{trace_id}]
│   │   └── user.py                   #   GET  /v1/users/me（占位）
│   │
│   ├── services/                     # 服务层（业务编排）
│   │   ├── sales_pitch_service.py    #   文本块构建 + agent.ainvoke + 审计编排
│   │   └── request_audit.py          #   审计文档构造（纯函数）+ MySQL 写/查
│   │
│   ├── agent/                        # Agent 层（DeepAgent Harness）
│   │   ├── loader.py                 #   资源加载（.sales_pitch/ → RedisStore）+ build_agent()
│   │   └── middleware.py             #   AlwaysLoadMemoryMiddleware（每次调用重载记忆）
│   │
│   ├── llm/                          # LLM 工厂层
│   │   └── factory.py                #   双通道 LLM（DashScope 主 + 安踏网关 fallback）+ 压缩 LLM
│   │
│   └── infra/                        # 基础设施层（全部静默降级）
│       ├── redis.py                  #   Redis checkpointer（短期记忆）+ store（长期记忆）
│       └── mysql.py                  #   MySQL 审计客户端（启动自动建表 request_audit）
│
├── .sales_pitch/                     # Agent 资源目录（启动时加载到 RedisStore，重启生效）
│   ├── AGENTS.md                     #   → /memory/AGENTS.md（角色 + 工作原则 + 多轮规则）
│   ├── SOUL.md                       #   → /soul/{warm,professional,concise}（话术风格预设）
│   └── skills/sales-pitch/SKILL.md   #   → /skills/sales-pitch/SKILL.md（撰写要求 + 不适用场景）
│
├── config/api_keys.yaml              # API Key 白名单（不入 Git）
├── config.yaml                       # 主配置文件（鉴权/LLM/Redis/MySQL/审计/日志）
├── requirements.txt                  # Python 依赖
├── Dockerfile                        # python:3.12-slim 多阶段构建
├── deployment{,-dev,-prod}.yaml      # K8s 部署（prod: f-aifit / dev: mgs-d）
├── tests/                            # pytest 单元测试（88 基线，全部 mock，不依赖外部资源）
└── web/                              # 前端 Vue 3 + Vite（详见 web/README.md，不在后端规范范围）
```

## 依赖规则

依赖方向固定单向：

```text
models / config  →  infra  →  llm  →  agent  →  services  →  routers  →  main
```

| 层 | 允许依赖 | 禁止依赖 |
|---|---|---|
| `models` | 无（纯 Pydantic + 清理函数） | 任何内部模块 |
| `config` | 无（独立读 yaml / 环境变量） | 任何内部模块 |
| `infra` | config | services / agent / llm / routers |
| `llm` | config | services / agent / infra / routers |
| `agent` | config、infra、llm | services / routers |
| `services` | models、config、agent、infra、services | routers、main |
| `routers` | models、config、auth、services | **agent / llm / infra（直接依赖）** |
| `main` | routers、config、logging_config、api_debug | agent / llm / infra（直接依赖） |

## Agent 装配（agent/loader.py）

```python
agent = create_deep_agent(
    model=llm,                    # 必须是 BaseChatModel 子类（primary ChatOpenAI，fallback 挂 _fallback_model 属性）
    system_prompt="...",          # FILA 金牌导购角色
    skills=["/skills/"],          # 从 RedisStore 加载 Skill 定义
    middleware=[
        AlwaysLoadMemoryMiddleware,   # 每次 invoke 重新加载 AGENTS.md（去掉原版跳过逻辑）
        SummarizationMiddleware,      # token > 50000 自动压缩，保留最近 10 条
    ],
    permissions=[FilesystemPermission(deny write, /skills/** /memory/** /soul/**)],
    backend=store_backend,        # StoreBackend(store, namespace=("fileSystem",))
    store=store, checkpointer=checkpointer,
)
```

装配发生在 `routers/sales_pitch.py` 模块级 `_init_agent_stack()`（import 时执行一次）：
`init_redis() → load_resources() → create_sales_pitch_llm() → build_agent() → SalesPitchService`。
Redis 不可用 → `_pitch_svc = None` → 请求 503（见 architecture/data-flow.md 降级表）。

## 会话与记忆模型

| 概念 | 载体 | Key |
|---|---|---|
| 请求追踪 | `request.state.trace_id`（中间件生成，回写 `X-Trace-Id`） | uuid4 hex |
| 多轮对话 | LangGraph thread_id ← `session_id` | `sp_checkpointer` 前缀 |
| Agent 资源 | RedisStore 文件系统 | `sp_store` 前缀，namespace `("fileSystem",)` |
| 请求审计 | MySQL `request_audit` 表（启动自动建表） | trace_id / session_id 索引 |

## 配置模型

- `config.yaml` 为唯一配置入口；环境变量优先覆盖（`MYSQL_URL` / `REDIS_HOST` / `REDIS_PORT` / `DASHSCOPE_API_KEY` / `ANTA_LLM_API_KEY` / `FILA_AGENT_DEBUG_API_IO`）
- `load_config()` 采用 mtime 缓存：文件变化自动重载，无需重启
- 审计双开关：`request_audit.enabled`（功能总开关，false 时不建连接）+ `mysql.url` 为空时静默降级
