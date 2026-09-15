## Context

当前 `session_id` 为可选入参，缺失时由 `uuid.uuid4().hex` 随机生成，映射为 LangGraph `thread_id` 实现多轮对话。`guide_id` 为可选字段，用于导购身份校验，与 session_id 完全正交。`customer` 整体可选，不含会员标识字段。

本次改造使 session_id 成为 `guide_num` 和 `customer.union_id` 的确定性派生值，全局将 `guide_id` 重命名为 `guide_num`。

## Goals / Non-Goals

**Goals:**
- session_id 由 `guide_num` + `customer.union_id` 确定性生成，同导购同会员永远共享对话上下文
- `guide_id` 全局重命名为 `guide_num`，包括请求模型、导购校验、配置文件、审计日志
- `customer` 改为必传，`union_id` 为必传字段
- 前端页面同步适配新字段结构

**Non-Goals:**
- 不改变 LangGraph checkpointer/store 的底层存储机制（仍用 Redis）
- 不引入会话过期或清理机制（依赖 SummarizationMiddleware 的上下文压缩）
- 不改变 `guide_auth` 的开关控制语义和鉴权执行顺序
- 不改变审计 MySQL 表结构（`session_id` 列仍为 `VARCHAR(128)`，足够容纳 `{guide_num}_{union_id}`）

## Decisions

### D1: session_id 生成格式为 `{guide_num}_{union_id}`

**决策**: 使用下划线 `_` 作为分隔符拼接 `guide_num` 和 `union_id`，例：`G001_U_abc123`。

**理由**: 下划线在 Redis key、URL、日志中均无需转义，比 `:` 在部分系统（如 Windows 路径）中兼容性更好。`guide_num` 和 `union_id` 本身不含下划线（工号为数字/字母，union_id 为平台分配的标识），不存在歧义。

**备选方案**: `{guide_num}:{union_id}`（冒号分隔）——可读性略好，但冒号在某些 URL 编码场景需额外处理。

### D2: 删除 `session_id` 入参而非保留覆盖

**决策**: 从 `SalesPitchRequest` 中彻底删除 `session_id` 字段，调用方无法覆盖。

**理由**: 保留覆盖能力会增加接口复杂度，且业务场景不需要调用方控制 session_id 的值。确定性生成已满足所有需求。

**备选方案**: 保留 `session_id` 作为可选覆盖——增加了灵活性但也增加了误用风险，且当前无此业务需求。

### D3: `union_id` 放在 `customer` 对象内而非 `SalesPitchRequest` 顶层

**决策**: `union_id` 作为 `SalesPitchCustomerInfo` 的必传字段，`customer` 整体改为必传。

**理由**: `union_id` 是会员标识，语义上属于顾客信息，放在 `customer` 内更自然。虽然这要求 `customer` 必须传入，但既然 session_id 生成依赖它，必传是合理的。

**备选方案**: 将 `union_id` 提升到 `SalesPitchRequest` 顶层与 `guide_num` 平级——语义分散，customer 仍可选但 session_id 生成依赖顶层字段，逻辑不如放在 customer 内清晰。

### D4: `guide_id` → `guide_num` 全局重命名

**决策**: 在整个代码库和配置文件中统一将 `guide_id` 重命名为 `guide_num`，包括：
- `backend/models.py`：`SalesPitchRequest.guide_id` → `guide_num`（改为必传 `str`）
- `backend/guide_auth.py`：`verify_guide_identity(guide_id)` → `verify_guide_identity(guide_num)`，`GuideUserStore.get` 参数更新
- `backend/config.py`：`get_guide_auth_config` 解析 `guide_num` key
- `config.yaml`：`guide_auth.users` 条目 key 从 `guide_id` 改为 `guide_num`
- `backend/routers/sales_pitch.py`：`body.guide_id` → `body.guide_num`
- `backend/services/sales_pitch_service.py`：审计 input 块中 `guide_id` → `guide_num`
- `openspec/specs/guide-identity/spec.md`：同步更新（archive 时合并）

**理由**: 导购工号（`guide_num`）比泛化的 `guide_id` 更准确地描述了字段的业务含义，减少与系统内部标识符的混淆。

### D5: `guide_num` 改为必传（不再依赖 `guide_auth.enabled` 开关决定是否校验）

**决策**: `guide_num` 在 `SalesPitchRequest` 中改为必传 `str` 字段（`Field(min_length=1)`），Pydantic 层校验。`guide_auth.enabled` 开关仅控制存在性校验（是否在 mock 用户列表中），不再控制字段是否必传。

**理由**: `guide_num` 现在是 session_id 的必要组成部分，缺失则无法生成 session_id，因此无论 `guide_auth.enabled` 状态如何都必须传入。

## Risks / Trade-offs

**[会话上下文永久累积]** → 同导购同会员的对话会永远累积在同一个 LangGraph checkpoint 中。Mitigation: `SummarizationMiddleware` 在超过 `trigger_tokens`（50000 tokens）时自动压缩上下文，保留最近 `keep_messages`（10 条）消息，避免无限膨胀。

**[对外 API Breaking Change]** → 删除 `session_id` 入参、重命名 `guide_id`、`customer` 改为必传，均为 breaking change。Mitigation: 调用方（微导购前端）需同步升级；如有第三方调用方需提前通知。可在接口版本不变的情况下通过文档明确变更。

**[session_id 长度]** → `{guide_num}_{union_id}` 长度须控制在 MySQL `VARCHAR(128)` 内。Mitigation: `guide_num` 通常 4~10 字符，`union_id`（微信 unionid）通常 28 字符，拼接后远小于 128 字符，无风险。

## Migration Plan

1. 后端改造（模型、服务、校验、配置）→ 单元测试同步更新 → 确保全部测试通过
2. 前端页面适配（去掉 session_id localStorage，新增 union_id 表单字段，字段名更新）
3. `config.yaml` 更新 mock 用户列表 key
4. 部署后验证：发起两次相同 `guide_num` + `union_id` 的请求，确认 session_id 相同且对话上下文连续
