## Purpose

为营销话术生成接口提供确定性会话标识：系统根据请求体中的导购工号（`guide_num`）和会员标识（`customer.union_id`）拼接生成 `session_id`，作为 LangGraph thread_id 实现同导购同会员的持久会话上下文共享，调用方不再传入 session_id。

## Requirements

### Requirement: session_id 由系统确定性生成

系统 SHALL 根据请求体中的 `guide_num` 和 `customer.union_id` 自动拼接生成 session_id，格式为 `{guide_num}_{union_id}`；调用方 SHALL NOT 能够通过请求体传入或覆盖 session_id。

#### Scenario: 系统根据 guide_num 和 union_id 生成 session_id

- **WHEN** 请求体携带 `guide_num` 为 `"G001"`，`customer.union_id` 为 `"U_abc123"`
- **THEN** 系统生成 session_id 为 `"G001_U_abc123"`，作为 LangGraph thread_id 使用，并在响应体中返回该 session_id

#### Scenario: 相同导购和会员的多次请求共享会话上下文

- **WHEN** 两次请求均携带 `guide_num="G001"` 和 `customer.union_id="U_abc123"`
- **THEN** 两次请求使用相同的 session_id `"G001_U_abc123"`，LangGraph 对话历史自动累积共享

#### Scenario: 不同导购或不同会员使用独立会话

- **WHEN** 请求 A 携带 `guide_num="G001"` + `union_id="U_abc"`，请求 B 携带 `guide_num="G001"` + `union_id="U_xyz"`（或 `guide_num="G002"` + `union_id="U_abc"`）
- **THEN** 两次请求生成不同的 session_id，对话上下文相互独立

### Requirement: 请求体删除 session_id 入参

`SalesPitchRequest` SHALL NOT 包含 `session_id` 字段；请求体中携带的 `session_id` SHALL 被忽略（Pydantic 默认行为）或拒绝。

#### Scenario: 请求体携带 session_id 被忽略

- **WHEN** 调用方在请求体中携带 `"session_id": "some_value"`
- **THEN** 该字段被忽略，session_id 仍由系统根据 `guide_num` 和 `union_id` 生成

### Requirement: guide_num 必传

`SalesPitchRequest` SHALL 包含必传的 `guide_num` 字段（字符类型，导购工号）；缺失或空白 SHALL 被拒绝。

#### Scenario: 请求缺少 guide_num

- **WHEN** 请求体未携带 `guide_num` 或 `guide_num` 为空字符串
- **THEN** 接口返回 422（Pydantic 校验失败）或 400（业务校验），指示 `guide_num` 缺失

#### Scenario: guide_num 前后空白被剥离

- **WHEN** 请求体携带 `guide_num` 为 `" G001 "`
- **THEN** 空白被剥离，`guide_num` 视为 `"G001"`，用于 session_id 生成和导购校验

### Requirement: customer 必传且 union_id 必传

`SalesPitchRequest` 的 `customer` 字段 SHALL 为必传；`customer` 对象中的 `union_id` 字段 SHALL 为必传（字符类型，会员标识）；`customer` 缺失或 `union_id` 缺失/空白 SHALL 被拒绝。

#### Scenario: 请求缺少 customer

- **WHEN** 请求体未携带 `customer` 字段
- **THEN** 接口返回 422（Pydantic 校验失败），指示 `customer` 缺失

#### Scenario: customer 缺少 union_id

- **WHEN** 请求体携带 `customer` 但未包含 `union_id`，或 `union_id` 为空字符串
- **THEN** 接口返回 422（Pydantic 校验失败），指示 `union_id` 缺失

#### Scenario: union_id 前后空白被剥离

- **WHEN** 请求体携带 `customer.union_id` 为 `" U_abc123 "`
- **THEN** 空白被剥离，`union_id` 视为 `"U_abc123"`，用于 session_id 生成

### Requirement: session_id 写入审计日志

审计文档中的 `session_id` 字段 SHALL 记录系统生成的确定性 session_id（`{guide_num}_{union_id}`）；审计 input 块中的 `guide_id` 字段 SHALL 重命名为 `guide_num`。

#### Scenario: 审计记录包含生成的 session_id

- **WHEN** 请求处理完成（成功或失败），`guide_num="G001"`，`union_id="U_abc"`
- **THEN** 审计文档 `session_id` 字段为 `"G001_U_abc"`，`input.guide_num` 为 `"G001"`
