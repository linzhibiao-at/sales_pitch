## Why

当前 session_id 由调用方传入或由系统随机生成 UUID，与导购身份（guide_id）和会员身份无关联。这导致同一导购服务同一会员的多次对话无法自动关联上下文，会话管理缺乏业务语义。需要将 session_id 改造为由导购工号（guide_num）和会员标识（union_id）确定性生成，使会话天然绑定「哪个导购在服务哪个会员」。

## What Changes

- **BREAKING**: 请求体删除 `session_id` 字段，session_id 由系统根据 `guide_num` + `union_id` 自动生成（格式 `{guide_num}_{union_id}`）
- **BREAKING**: 请求体 `guide_id` 字段重命名为 `guide_num`（导购工号，字符类型，必传）
- **BREAKING**: `customer` 字段从可选改为必传，其中新增 `union_id` 字段（会员标识，字符类型，必传）
- 导购身份校验模块（guide_auth）全局同步 `guide_id` → `guide_num` 重命名，包括配置文件中 mock 用户列表的 key
- 审计日志中 session_id 和 guide_id 相关字段同步更新
- 前端话术生成页面去掉 session_id localStorage 管理逻辑，表单新增 union_id 输入，字段名同步更新

## Capabilities

### New Capabilities
- `session-management`: session_id 确定性生成规则——系统根据请求体中的 `guide_num` 和 `customer.union_id` 拼接生成 session_id（格式 `{guide_num}_{union_id}`），作为 LangGraph thread_id 实现同导购同会员的持久会话共享；删除调用方传入 session_id 的能力

### Modified Capabilities
- `guide-identity`: `guide_id` 全局重命名为 `guide_num`；`customer` 从可选改为必传且 `union_id` 必传；原有校验逻辑（存在性校验、开关控制、鉴权顺序）不变，仅字段名变更

## Impact

- **对外 API (BREAKING)**: `POST /v1/sales-pitch/generate` 请求体结构变更——删除 `session_id`，`guide_id` → `guide_num`，`customer` 必传且含必传 `union_id`；响应体仍返回 `session_id` 但值为系统生成
- **后端模型**: `backend/models.py` — `SalesPitchRequest` 和 `SalesPitchCustomerInfo` 结构变更
- **后端服务**: `backend/services/sales_pitch_service.py` — session_id 生成逻辑变更
- **导购校验**: `backend/guide_auth.py`、`backend/config.py`、`config.yaml` — 字段名全局替换
- **审计**: `backend/services/request_audit.py`、`backend/infra/mysql.py` — 字段引用更新
- **前端**: `web/src/views/GenerateView.vue`、`web/src/api/index.js` — 表单字段和 localStorage 逻辑更新
- **测试**: `tests/` 下相关测试用例需同步更新字段名和断言
