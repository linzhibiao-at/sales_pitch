## Purpose

为营销话术生成等对外 B2B 接口提供导购级身份校验：请求体携带 `guide_id`，系统校验其存在于用户数据源（当前为配置驱动的 mock 数据源，暂不建数据库 user 表），不存在则拒绝请求，防止任意标识冒充导购调用接口。

## ADDED Requirements

### Requirement: 请求携带导购身份标识

导购校验启用时，对外 B2B 接口的请求体 SHALL 携带非空 `guide_id` 字段作为导购身份标识；`guide_id` 前后的空白字符在比较前 SHALL 被剥离。

#### Scenario: 启用校验时缺失 guide_id

- **WHEN** 导购校验启用，请求体未携带 `guide_id`
- **THEN** 接口返回 400 与统一错误 envelope `{"code": 400, "message": ..., "trace_id": ...}`，message 指示 `guide_id` 缺失，请求不进入业务处理

#### Scenario: guide_id 为空白字符串视为缺失

- **WHEN** 导购校验启用，请求体携带 `guide_id` 为空白字符串（如 `"  "`）
- **THEN** 接口返回 400 与统一错误 envelope，message 指示 `guide_id` 缺失

#### Scenario: guide_id 前后空白被剥离后校验

- **WHEN** 导购校验启用，请求体携带 `guide_id` 为 `" G001 "`，且 `G001` 存在于用户数据源
- **THEN** 校验通过，请求正常处理

### Requirement: 导购存在性校验

导购校验启用时，系统 SHALL 校验请求体 `guide_id` 存在于用户数据源；不存在 SHALL 拒绝请求。

#### Scenario: guide_id 存在于用户数据源

- **WHEN** 导购校验启用，请求体携带的 `guide_id` 存在于用户数据源
- **THEN** 校验通过，请求继续正常业务处理，话术生成结果与既有行为一致

#### Scenario: guide_id 不存在于用户数据源

- **WHEN** 导购校验启用，请求体携带的 `guide_id` 不存在于用户数据源
- **THEN** 接口返回 401 与统一错误 envelope `{"code": 401, "message": ..., "trace_id": ...}`，message 指示导购不存在，请求不进入业务处理

### Requirement: 校验开关与向后兼容

导购身份校验 SHALL 受配置开关控制；开关关闭时系统 SHALL 不做任何导购校验，既有请求行为保持不变。

#### Scenario: 开关关闭且请求不含 guide_id

- **WHEN** 导购校验开关关闭，请求体不携带 `guide_id`
- **THEN** 请求正常处理，与既有行为一致，不因导购身份报错

#### Scenario: 开关关闭且 guide_id 未知

- **WHEN** 导购校验开关关闭，请求体携带一个不存在于用户数据源的 `guide_id`
- **THEN** 请求正常处理，不因导购身份被拒绝

### Requirement: Mock 用户数据源

用户数据源 SHALL 由配置文件提供（mock 用户列表），不依赖数据库建表；数据源 SHALL 以统一的存在性查询语义暴露，使实现可在不改变本规格所述外部行为的前提下替换为数据库 user 表查询。

#### Scenario: 配置中的用户校验通过

- **WHEN** mock 用户列表包含导购 `G001`，启用校验的请求携带 `guide_id` 为 `G001`
- **THEN** 导购存在性校验通过

#### Scenario: 配置变更后生效

- **WHEN** mock 用户列表在配置文件中新增或移除导购（不重启服务）
- **THEN** 后续请求按更新后的用户列表进行校验

#### Scenario: 用户列表为空时全部拒绝

- **WHEN** 导购校验启用且 mock 用户列表为空，请求携带任意 `guide_id`
- **THEN** 导购存在性校验失败，返回 401

### Requirement: 校验范围

导购身份校验 SHALL 应用于所有对外 B2B 业务接口（当前为 `POST /v1/sales-pitch/generate`）；运维与健康检查接口（如 `/health`、审计查询接口）SHALL 不受导购校验影响。应用级鉴权（API Key）SHALL 先于导购校验执行，既有应用级鉴权行为不变。

#### Scenario: 话术生成接口受校验保护

- **WHEN** 导购校验启用，调用 `POST /v1/sales-pitch/generate` 携带不存在于数据源的 `guide_id`
- **THEN** 返回 401，统一错误 envelope

#### Scenario: 运维接口不受导购校验影响

- **WHEN** 导购校验启用，调用 `/health` 或审计查询等运维接口
- **THEN** 不做导购校验，正常响应

#### Scenario: 应用级鉴权优先于导购校验

- **WHEN** 请求携带无效 API Key 但 `guide_id` 合法（存在于数据源）
- **THEN** 仍按既有应用级鉴权行为返回 401（API key 错误），导购校验不改变应用级鉴权结果
