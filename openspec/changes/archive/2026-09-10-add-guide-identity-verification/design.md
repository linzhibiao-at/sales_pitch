# 技术设计：导购身份校验（mock 数据源）

## Context

见 proposal.md（Why）。当前鉴权仅有应用级（`backend/auth.py` 的 API Key 绑定 `app_id`），请求体字段级校验（`app_id` 缺失/白名单/Key 绑定）在 `backend/routers/sales_pitch.py` 处理器内完成。MySQL 仅用于 `request_audit` 审计表（`backend/infra/mysql.py`），无 user 表。错误响应统一由 `backend/main.py` 的异常处理器产出 envelope `{"code", "message", "trace_id"}`。配置经 `backend/config.py` 的 `load_config()`（mtime 缓存，天然热加载）。

约束：暂不建 MySQL user 表，以 mock 数据源落地；不引入新依赖。

## Goals / Non-Goals

**Goals:**

- `guide_id` 校验链路完整落地：请求体字段 → 校验 → 401/400 拒绝，且开关可控、向后兼容
- 数据源查询语义统一（按 `guide_id` 存在性查询），未来可平滑替换为 MySQL user 表实现
- 校验组件可被未来所有对外 B2B 接口一行复用

**Non-Goals:**

- 不建数据库 user 表、不写 DDL、不做用户管理（增删改查接口）
- 不做导购级限流/配额（限流仍按 `app_id` 应用级）
- 不引入 guide token/签名等强凭证机制（mock 阶段 `guide_id` 即凭证，见 Risks）
- 不改审计表结构（`guide_id` 随 `input_json` 记录，不加列）

## Decisions

### D1. 挂接方式：路由处理器内调用辅助函数（非 FastAPI Depends）

`backend/guide_auth.py` 提供纯函数 `verify_guide_identity(guide_id) -> dict | None`（raise `HTTPException`），在 `/v1/sales-pitch/generate` 处理器内、`app_id` 三层校验之后调用。

理由：

- 与现有代码一致——body 字段级校验（`app_id` 缺失 400、白名单 401、Key 绑定 401）本就在处理器内做；`verify_api_key` Depends 只负责 header 级。`guide_id` 是 body 字段，走同层
- 校验顺序干净：应用级鉴权（Depends `verify_api_key`，含限流）→ 应用级 body 校验 → **用户级导购校验** → 业务调用，逐层递进
- 纯函数无 `Request` 依赖，单测无需 TestClient

备选（否决）：`Depends(verify_guide)` 依赖内 `await request.json()` 读 raw body——依赖 starlette body 缓存的隐式行为，且依赖先于 body Pydantic 校验执行，错误暴露顺序（如畸形 body 下 400 vs 422）不够直观；与现有 body 校验风格不一致。

### D2. `guide_id` 模型字段：`Optional[str]` + strip 校验器

`SalesPitchRequest` 新增 `guide_id: Optional[str] = None`，加入既有 `_strip_text` 校验器列表（与 `pitch_style`/`channel` 同款空白剥离）。

理由：开关关闭时老调用方不传 `guide_id` 也不报错（向后兼容，对齐 `auth.enabled` 缺省 False 的灰度惯例）。备选（否决）：模型必填 `str`——开关关闭时仍会 422 拒绝缺字段请求，属破坏性变更。

### D3. 配置结构与默认值

`config.yaml` 新增段（`backend/config.py` 新增 `get_guide_auth_config()` 读取，风格对齐 `get_auth_config()`）：

```yaml
guide_auth:
  enabled: true          # 缺省 false（键缺失=不校验，向后兼容）
  users:                 # mock 用户列表（暂代 user 表）
    - guide_id: "G001"
      name: "测试导购一"
    - guide_id: "G002"
      name: "测试导购二"
```

- 读取函数返回归一化 `{enabled: bool, users: [{guide_id: str, ...}]}`：`guide_id` 剥空白、空 `guide_id` 条目丢弃
- 用户列表为 dict 列表（非纯字符串列表）：为未来 user 表字段（name/status/store_id 等）留形状，mock 条目多余字段原样保留
- 热加载：每次校验调用 `get_guide_auth_config()` → 走 `load_config()` mtime 缓存，改 `config.yaml` 即生效（与 `allowed_app_ids` 同模式），**不在初始化时缓存用户列表**

仓库内 `config.yaml` 显式 `enabled: true` 并内置两个 mock 导购，便于联调演示；生产灰度可改 `false` 回退旧行为。

### D4. 组件位置：独立 `backend/guide_auth.py`

`backend/auth.py`（293 行）职责是应用级 API Key + 限流排队；导购校验是用户级关注点，独立成 `backend/guide_auth.py`：

- `GuideUserStore`：`get(guide_id) -> dict | None` 存在性查询（进程级单例 + getter，对齐 `auth.py` 的单例风格）
- `verify_guide_identity(guide_id) -> dict | None`：开关关闭 → 返回 `None` 放行；启用 → 缺失/空白 400 `guide_id required`，未知 401 `guide not found`

错误文案对齐既有风格（`app_id required` / `invalid API key`）；envelope 由 `main.py` 既有 `HTTPException` 处理器统一产出，无需新代码。

### D5. 数据源替换扩展点（未来 MySQL user 表）

`GuideUserStore.get()` 是唯一数据访问入口。未来替换为 MySQL 实现时：实现同签名方法（查 `user` 表 `WHERE guide_id = %s`），复用 `backend/infra/mysql.py` 的连接管理模式；因路由为 async 而 pymysql 同步，届时需 TTL 进程内缓存 + `run_in_executor`（或 async 驱动），避免每请求同步查库阻塞事件循环。本次 mock 为纯内存配置读取，无此问题。

### D6. 校验命中信息与审计

- 命中用户写入 `request.state.guide`（对齐 `request.state.caller` 惯例），供 handler/未来功能取用
- `SalesPitchService._write_audit()` 的 `input_block` 增加 `"guide_id"` 字段（开关关闭时为 `null`），随 `input_json` 落审计表，不改表结构

## Risks / Trade-offs

- [mock 阶段 `guide_id` 即凭证，可被枚举冒用] → 已知限制：本阶段目标是打通校验链路，非强认证；生产化前替换为 user 表实现并考虑 token/签名（见 Non-Goals）
- [`enabled: true` 对现有调用方是行为变更（须补 `guide_id`）] → 回滚 = 配置改 `false`（秒级，无需重启/发版）；`config.yaml` 注释说明
- [多 worker 下 mock 列表一致性] → 配置为共享文件，mtime 缓存 per-worker，短暂不一致窗口与 `api_keys.yaml` 热加载同模式，可接受
- [依赖读取顺序耦合：未来新增 B2B 接口可能漏挂校验] → 设计上调用点集中、一行接入；code review 约定新 B2B 路由必须调用 `verify_guide_identity`
- [审计 `input_json` 记录 `guide_id` 无独立索引列] → 按 `input_json` LIKE 查询效率低；如需按导购检索，未来加列迁移（本次明确不做，见 Non-Goals）

## Migration Plan

1. 合并代码 + `config.yaml` 新增 `guide_auth` 段（`enabled: true` + mock 用户）
2. 调用方（`micro_guide` 等）在请求体补 `guide_id`（联调期可先 `enabled: false` 观察，再开启）
3. 回滚：`guide_auth.enabled: false`（立即恢复旧行为）或回退版本；无数据库变更需要清理

## Open Questions

- 未来 MySQL `user` 表的表结构（字段、status 停用语义、索引）——mock 阶段只做存在性查询，不影响本次规格与任务，接入前再定
- mock 用户是否需要 `status: inactive` 停用语义——当前配置形状已兼容多余字段，规格仅约定存在性校验，需要时再扩展
