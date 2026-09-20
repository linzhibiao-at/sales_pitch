## Purpose

为 AI邀约场景补齐话术生成接口的入参契约与生成行为：以分享方式（单品/成套）区分文案结构，注入已选门店活动与顾客券，承载会员等级与积分，并提供四档文案语气预设，使微导购邀约工作台可直接对接生成邀约文案。

## ADDED Requirements

### Requirement: share_mode 分享方式请求字段

`SalesPitchRequest` SHALL 包含可选字段 `share_mode`，取值 SHALL 为 `item`（单品）或 `set`（成套），其他取值 SHALL 被拒绝；字段缺省时 SHALL 保持现有通用多商品生成行为；请求 `products` 数组的第一件 SHALL 作为主款识别约定。

#### Scenario: 携带 share_mode=item

- **WHEN** 请求体携带 `"share_mode": "item"` 与商品列表
- **THEN** 请求正常处理，话术按单品结构生成（围绕主款商品的名称与价格展开，不引入搭配件叙述）

#### Scenario: 携带 share_mode=set

- **WHEN** 请求体携带 `"share_mode": "set"` 与商品列表
- **THEN** 请求正常处理，话术按成套结构生成（以主款为基点呈现整套搭配，含搭配件商品名列表）

#### Scenario: 未携带 share_mode

- **WHEN** 请求体不包含 `share_mode`
- **THEN** 请求正常处理，生成行为与本次变更前一致

#### Scenario: share_mode 为非法取值

- **WHEN** 请求体携带 `"share_mode": "pair"` 等非 item/set 取值
- **THEN** 接口返回 422（Pydantic 校验失败），指示字段取值非法

### Requirement: promotions 已选活动请求字段

`SalesPitchRequest` SHALL 包含可选字段 `promotions`（对象数组，门店 POS 已选活动）；每个对象 SHALL 包含 `promo_id`（活动标识，必填、清理后非空）与 `copy`（活动展示文案，必填、清理后非空），可选包含 `name`（活动名）；数组长度 SHALL NOT 超过 10；字符串字段 SHALL 沿用现有自由文本清洗策略（剥除 HTML 标签与 lone surrogate）。未携带或为空数组 SHALL 均不产生活动注入。

#### Scenario: 携带合法 promotions

- **WHEN** 请求体携带 `promotions` 含 `{"promo_id": "mixian", "name": "万象城1000-200", "copy": "现在万象城有【部分整单满减】满1000减200…"}`
- **THEN** 请求正常处理，活动参与话术生成

#### Scenario: copy 缺失或清理后为空

- **WHEN** promotions 某项缺失 `copy`、为空字符串或纯空白
- **THEN** 接口返回 422（Pydantic 校验失败），指示 `copy` 非空要求

#### Scenario: promo_id 缺失或清理后为空

- **WHEN** promotions 某项缺失 `promo_id` 或值为纯空白
- **THEN** 接口返回 422（Pydantic 校验失败）

#### Scenario: 超过数量上限

- **WHEN** promotions 数组长度超过 10
- **THEN** 接口返回 422（Pydantic 校验失败）

#### Scenario: copy 中的 HTML 标签被剥除

- **WHEN** promotions 某项 `copy` 包含 HTML 标签（如 `<script>`）
- **THEN** 标签被剥除后参与生成，不因内容过滤导致话术生成失败

#### Scenario: 未携带 promotions

- **WHEN** 请求体不包含 `promotions` 或为空数组
- **THEN** 请求正常处理，不注入活动信息，生成行为与本次变更前一致

### Requirement: coupon_names 已选券请求字段与兜底

`SalesPitchRequest` SHALL 包含可选字段 `coupon_names`（字符串数组，已核验且已选的顾客券名）；数组长度 SHALL NOT 超过 20，单项清理后长度 SHALL NOT 超过 100 字符；各项 SHALL 沿用现有自由文本清洗策略；清理后为空的项 SHALL 被剔除。字段缺省时 SHALL 不产生券注入且不产生兜底表述；显式携带且清理后为空（空数组或全部项目被剔除）时，生成 SHALL 按 PRD 使用"会员专属优惠"兜底表述。

#### Scenario: 携带非空券名

- **WHEN** 请求体携带 `"coupon_names": ["500元生日券"]`
- **THEN** 请求正常处理，话术体现该券可用

#### Scenario: 显式空列表触发兜底

- **WHEN** 请求体显式携带 `"coupon_names": []`
- **THEN** 生成的话术以"会员专属优惠"兜底表述顾客权益，不罗列具体券名

#### Scenario: 未携带 coupon_names

- **WHEN** 请求体不包含 `coupon_names`
- **THEN** 请求正常处理，不注入券信息且不产生兜底表述，生成行为与本次变更前一致

#### Scenario: 超量或单项超长被拒绝

- **WHEN** coupon_names 数组长度超过 20，或某项清理后长度超过 100 字符
- **THEN** 接口返回 422（Pydantic 校验失败）

### Requirement: customer 会员等级与积分字段

`SalesPitchCustomerInfo` SHALL 包含可选字段 `member_level`（会员等级，自由文本，沿用现有清洗策略）与 `points`（会员积分，非负整数）；未携带时 SHALL NOT 在【顾客信息】块中产生对应行。

#### Scenario: 携带会员等级与积分

- **WHEN** customer 携带 `"member_level": "金卡会员"` 与 `"points": 1260`
- **THEN** 两者以"会员等级""积分"行注入【顾客信息】块，参与话术生成

#### Scenario: points 为负数

- **WHEN** customer 携带 `"points": -5`
- **THEN** 接口返回 422（Pydantic 校验失败）

#### Scenario: 未携带会员等级与积分

- **WHEN** customer 不包含 `member_level` 与 `points`
- **THEN** 【顾客信息】块不含对应行，生成行为与本次变更前一致

### Requirement: pitch_style 四语气预设

系统 SHALL 将 `pitch_style` 的四档语气取值「亲切自然」「活力潮流」「专业尊贵」「简约高效」识别为预设，并映射为对应的语气写作指令注入【话术要求】块；现有预设（warm/professional/concise）与未知自由描述取值的既有处理 SHALL 保持不变；`pitch_style` 缺省时 SHALL 不产生风格行。

#### Scenario: 携带四语气之一

- **WHEN** 请求体携带 `"pitch_style": "活力潮流"`
- **THEN** 【话术要求】块包含「活力潮流」对应的语气写作指令，话术按该语气生成

#### Scenario: 未知自由描述保持透传

- **WHEN** 请求体携带 `"pitch_style": "小红书种草风"`
- **THEN** 取值原样注入【话术要求】块，与本次变更前一致

#### Scenario: 现有预设保持兼容

- **WHEN** 请求体携带 `"pitch_style": "warm"`
- **THEN** 按既有映射注入【话术要求】块，与本次变更前一致

### Requirement: 促销活动注入生成提示词

`promotions` 清理后非空时，系统 SHALL 以独立文本块【促销活动】注入生成用户消息，逐项给出活动名称（如有）与展示文案原文（活动标识不注入提示词，仅记入审计快照）；生成话术 SHALL 仅引用所提供的活动文案、SHALL NOT 编造其他活动内容；块位置 SHALL 在【商品信息】之后、【话术要求】之前；缺省或空数组 SHALL NOT 产生该块。

#### Scenario: 活动以独立块注入

- **WHEN** 请求携带非空 promotions
- **THEN** 生成用户消息包含【促销活动】块且含各项 `copy` 原文，位于【商品信息】块之后、【话术要求】块之前

#### Scenario: 未携带时不注入

- **WHEN** 请求未携带 promotions 或为空数组
- **THEN** 生成用户消息不包含【促销活动】块

### Requirement: 顾客券注入生成提示词

`coupon_names` 清理后非空时，系统 SHALL 以独立文本块【顾客优惠券】注入生成用户消息并列出全部券名；显式携带且清理后为空时，该块 SHALL 包含"会员专属优惠"兜底指示；块位置 SHALL 在【促销活动】（无则【商品信息】）之后、【话术要求】之前；字段缺省时 SHALL NOT 产生该块。

#### Scenario: 券名以独立块注入

- **WHEN** coupon_names 清理后非空
- **THEN** 生成用户消息包含【顾客优惠券】块并列出全部券名

#### Scenario: 空列表产生兜底指示

- **WHEN** 显式携带 coupon_names 且清理后为空
- **THEN** 生成用户消息包含【顾客优惠券】块，块内含"会员专属优惠"兜底指示

#### Scenario: 未携带时不注入

- **WHEN** 请求未携带 coupon_names
- **THEN** 生成用户消息不包含【顾客优惠券】块

### Requirement: 新入参写入审计快照

审计文档 input 快照 SHALL 记录新入参：`share_mode`（未携带时为 null）、`promotions`（未携带或空数组时为 null，否则记录各项 promo_id/name/copy）、`coupon_names`（未携带时为 null，显式空列表记录为空数组）；`customer` 快照 SHALL 在携带时包含 `member_level` 与 `points`，未携带时不出现。

#### Scenario: 审计记录新入参

- **WHEN** 携带 share_mode、promotions、coupon_names、member_level、points 的请求处理完成（成功或失败）
- **THEN** 审计 input 依次记录对应值（自由文本为清理后原文，活动记录 id/name/copy 结构）

#### Scenario: 未携带时记录 null 或不出现

- **WHEN** 请求未携带新字段
- **THEN** 审计 input 中 share_mode/promotions/coupon_names 为 null，customer 快照不含 member_level 与 points

### Requirement: 现有调用方兼容性

未携带任何新字段的请求 SHALL 保持与本次变更前一致的提示词构成与校验结果。

#### Scenario: 仅携带既有字段

- **WHEN** 请求体与变更前的合法请求完全一致（不含 share_mode/promotions/coupon_names/member_level/points）
- **THEN** 生成的提示词文本块构成与审计输入与本次变更前一致（不新增任何文本块与风格变化）
