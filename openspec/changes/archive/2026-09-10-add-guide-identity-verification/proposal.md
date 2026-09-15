# 变更提案：导购身份校验（guide identity verification）

## Why

当前系统仅有应用级鉴权（API Key 绑定 `app_id`），只回答"哪个应用在调用"，无法识别"哪位导购在操作"。业务上话术生成以导购为主体（调用方 `micro_guide` 即导购端），缺少导购级身份校验意味着任意字符串都能冒充导购调用接口。本次先以**模拟数据源**落地校验链路（暂不建 MySQL user 表），为后续接入真实 user 表预留扩展点。

## What Changes

- 请求体 `SalesPitchRequest` 新增 `guide_id` 字段（导购身份标识）
- 新增导购身份校验组件（可复用校验函数，路由处理器内调用），校验 `guide_id` 是否存在于用户数据源
- 用户数据源以 **mock 实现**（配置驱动的用户列表，暂不建数据库 user 表）；定义统一查询接口，为后续 MySQL user 表实现预留扩展点
- 校验失败（导购不存在）→ 返回 401 与统一错误 envelope `{"code": 401, "message": ..., "trace_id": ...}`
- 校验逻辑设计为可复用组件，覆盖所有对外 B2B 接口（当前仅 `/v1/sales-pitch/generate`，未来接口同样一行调用）
- `config.yaml` 新增导购校验配置段（开关 + mock 用户列表），与现有 `auth`/`request_audit` 开关风格一致

## Capabilities

### New Capabilities

- `guide-identity`: 导购身份校验——`guide_id` 的传入契约、mock 用户数据源查询、校验失败的 401 错误行为、配置开关与向后兼容策略

### Modified Capabilities

（无——`openspec/specs/` 当前为空，无已有 capability 的需求变更）

## Impact

- **代码**：
  - `backend/models.py`：`SalesPitchRequest` 新增 `guide_id` 字段
  - `backend/config.py`：新增导购校验配置读取（开关、mock 用户列表）
  - `backend/guide_auth.py`（新增）：mock 用户存储 + 校验依赖
  - `backend/routers/sales_pitch.py`：挂接导购校验依赖
  - `config.yaml`：新增配置段
- **API 契约**：`/v1/sales-pitch/generate` 请求体新增 `guide_id`；开关开启后缺失/未知 `guide_id` 请求被拒绝（开关默认行为在 design.md 中决策，保证向后兼容）
- **依赖/系统**：无新增第三方依赖；不涉及数据库变更（暂不建表）
- **测试**：`tests/` 新增导购校验单测；现有测试需适配 `guide_id` 字段

## 假设与澄清记录

- 用户确认：导购身份通过**请求体新增 `guide_id` 字段**传入
- 用户确认：**暂不建 user 表**，以模拟接口实现（mock 数据源）
- 用户确认：校验范围覆盖**所有对外 B2B 接口**（当前先落到 `/v1/sales-pitch/generate`，依赖可复用）
- 用户回答"用户存在返回401"与最初需求"user 不存在则返回报错"矛盾，按最初需求理解为：**导购不存在 → 401**（若理解有误请提出）
