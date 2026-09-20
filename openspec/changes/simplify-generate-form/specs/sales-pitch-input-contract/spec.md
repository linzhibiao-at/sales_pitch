## Purpose

定义 `/v1/sales-pitch/generate` 的入参字段契约：顾客信息、商品信息与请求级字段收敛到业务核心集，被移除字段不再参与校验与处理；旧调用方传入被移除字段时请求保持成功（兼容降级）；生成提示词与审计快照同步收敛，无被移除字段残留。

## ADDED Requirements

### Requirement: 顾客信息入参字段集合

`SalesPitchCustomerInfo` SHALL 仅包含可选字段 `nickname`、`gender`、`age`、`member_level`、`points` 与 `extra`；`union_id` SHALL 保持必填。`size_info`、`style_preference`、`scene`、`budget`、`notes` SHALL 从模型移除，传入时 SHALL 被忽略且不产生校验错误。

#### Scenario: 核心字段参与生成并保持既有行为

- **WHEN** 请求 customer 携带 `nickname`、`gender`、`age`、`member_level`、`points`
- **THEN** 【顾客信息】块依次包含称呼、性别、年龄段、会员等级、积分行，生成行为与本次变更前一致

#### Scenario: 被移除字段传入被忽略

- **WHEN** 请求 customer 携带 `"size_info": "M 码"`、`"style_preference": "复古运动"`、`"scene": "秋季通勤"`、`"budget": "500-800元"`、`"notes": "偏好红色"` 等被移除字段
- **THEN** 请求正常处理（不报 422），被移除字段不出现在【顾客信息】块与审计 customer 快照中

### Requirement: 商品信息入参字段集合

`SalesPitchProductInfo` SHALL 仅包含必填字段 `title`（清理后非空）、可选字段 `color` 与 `extra`。`sku_id`、`price`、`category`、`material`、`selling_points` SHALL 从模型移除，传入时 SHALL 被忽略且不产生校验错误。【商品信息】块 SHALL 仅注入商品名称、颜色（及 extra）行，不再输出货号、价格、类目、材质、卖点行。

#### Scenario: 商品块仅含核心行

- **WHEN** 请求商品携带 `"title": "轻量羽绒服"` 与 `"color": "米白"`
- **THEN** 【商品信息】块包含商品名与颜色行，且不出现货号、价格、类目、材质、卖点行

#### Scenario: 被移除字段传入被忽略

- **WHEN** 请求商品携带 `"sku_id": "F123"`、`"price": 399`、`"category": "羽绒服"`、`"material": "鹅绒"`、`"selling_points": "轻薄保暖"`
- **THEN** 请求正常处理（不报 422）；提示词无对应行；审计 products 快照不含这些键

### Requirement: 请求级字段集合

`SalesPitchRequest` SHALL 仅包含 `app_id`、`guide_num`、`customer`、`products`、`promotions`、`coupon_names`、`pitch_style`、`max_length`、`extra_prompt`；`share_mode` 与 `channel` SHALL NOT 作为入参字段。`channel` 传入时 SHALL 被忽略且不产生校验错误。【话术要求】块 SHALL 仅由风格行与长度行构成，不再输出分享方式行与渠道行。

#### Scenario: 渠道字段传入被忽略

- **WHEN** 请求携带 `"channel": "wechat"`
- **THEN** 请求正常处理（不报 422）；【话术要求】块不含渠道行；审计 input 顶层不含 channel

#### Scenario: 风格与字数行保持既有行为

- **WHEN** 请求携带 `pitch_style` 与 `max_length`，不含 channel
- **THEN** 【话术要求】块仅含风格行与长度行，与本次变更前一致

### Requirement: 被移除字段全链路无残留

请求携带任意被移除字段时，生成流程 SHALL 正常完成；生成用户消息的文本块 SHALL NOT 出现被移除字段对应的行式输出；审计 input 快照 SHALL NOT 包含 `share_mode`、`channel` 顶层键，customer 与 products 快照 SHALL NOT 包含各自被移除字段。

#### Scenario: 携带全套旧字段的请求

- **WHEN** 请求携带全部 12 个被移除字段（含 share_mode 与 channel）并成功生成
- **THEN** 提示词各文本块与审计 input 均无被移除字段残留，话术不引用价格、类目、尺码、场景等信息

#### Scenario: 不含被移除字段的标准请求回归

- **WHEN** 请求仅携带保留字段（核心集）
- **THEN** 生成与审计结果与本次变更前一致，无新增文本块或字段变化
