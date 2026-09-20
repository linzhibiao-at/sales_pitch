# Design: add-ai-invite-copy-inputs

## Context

话术生成链路：`POST /v1/sales-pitch/generate` → `SalesPitchRequest` 校验清洗 → 文本块拼装（现为【顾客信息】【商品信息】【话术要求】【补充要求】）→ DeepAgent（`{guide_num}_{union_id}` 映射 thread_id，多轮历史自动累积）→ 响应 + 审计快照（内存队列后台批量写 MySQL）。提示词资产位于 `.sales_pitch/`（`AGENTS.md` + `skills/sales-pitch/SKILL.md`）。

约束（决定本设计形态）：

- 现有调用方（web 控制台等）不得受影响：所有新入参可选、缺省时行为零差异。
- 服务端无商品/活动/券数据源，业务数据全部由调用方传入；生成侧禁止编造由现有提示词「禁止事项」覆盖。
- 在途变更 `add-extra-prompt-input` 已引入 `extra_prompt` 与【补充要求】块；本变更复用它承接"换一换"改写要求，不重复定义。
- PRD 6.6 的 camelCase 字段名属产品侧提议（PRD 待确认事项亦声明字段名待定），接口契约以本服务 snake_case 风格定稿。

## Goals / Non-Goals

**Goals:**

- 微导购邀约工作台第 4 步"AI 文案"可直接调用本接口生成单品/成套文案，覆盖已选活动、已核验券、会员等级/积分与四档语气。
- 以兼容性扩展方式交付：缺省请求的提示词构成与审计输入与现版本一致。

**Non-Goals:**

- PRD 6.5 AI 搭配接口、6.7 发送接口（非本服务职责）。
- web 控制台 UI 增加新字段控件（联调可用 curl 验证）。
- 会话隔离粒度改造（保持 `{guide_num}_{union_id}`，见 Risks）。

## Decisions

### D1 命名与兼容策略：顶层 snake_case 可选字段，缺省零差异

新字段一律可选（`Optional`），命名与现有契约风格一致：`share_mode`、`promotions`、`coupon_names`；`customer` 内新增 `member_level`、`points`。缺省时不产生任何新文本块、不改动校验结果。Pydantic 默认 `extra="ignore"`，服务回滚到旧版本时新字段被静默忽略，形成天然降级。

- 备选：按 PRD 原名 camelCase（memberId/shareMode/promoIds…）——与现有契约混用两种风格，否；新增 `tone` 独立字段——与 `pitch_style` 语义重叠，否（见 D3）。

### D2 商品与分享方式：`products` 对象数组 + `share_mode`，`products[0]` 为主款

`share_mode: Optional[Literal["item", "set"]]`；`item`=单品（只介绍主款）、`set`=成套（主款+搭配件整套呈现）。主款取 `products[0]`，不新增 `hero_sku_id`（与 PRD 6.6 的"单品=主款、成套=整套"约定一致，调用方控制数组顺序）。

- 备选：只接收 `product_ids`——话术需要名称/价格/卖点，服务端无商品库，否；显式 `hero_sku_id` ——多一个字段且与数组序冗余，否。

### D3 语气：扩展 `pitch_style` 预设，不新增 `tone` 字段

在 `_PITCH_STYLE_LABELS` 增加四个中文键（亲切自然/活力潮流/专业尊贵/简约高效 → 同名标签），【话术要求】块输出"- 风格: <标签>"；四档语气的写作定义补充到 `.sales_pitch` 提示词资产。现有三档英文预设（warm/professional/concise）与未知自由描述透传保持不变；缺省不产生风格行。默认"亲切自然"由调用方（微导购工作台）保证。

- 备选：新增 `tone` 字段——与 `pitch_style` 功能重叠，契约冗余，否；`tone` 替换 `pitch_style`——破坏兼容，否。

### D4 活动入参：`promotions` 对象数组，`{promo_id, name, copy}`，`copy` 必填

- `promo_id`：必填、strip 后非空；**不注入提示词**（避免内部标识泄漏进话术），仅用于审计溯源。
- `name`：可选（活动名），用于 prompt 行前缀。
- `copy`：必填、清洗（HTML/surrogate）+ strip 后非空——生成只允许引用该文案原文；调用方从 POS 促销接口取不到文案时无法使用本能力，属有意收紧。
- 数组上限 10（防御性）。新增模型类 `SalesPitchPromotionInfo`。

- 备选：与 PRD 一致只传 `promoIds`——服务端拼不出活动文案且禁止编造，活动段落直接缺失，否；字符串数组仅传文案——丢失审计溯源与展示名，否。

### D5 券入参：`coupon_names`，缺省与显式空列表语义分离

- 缺省（不携带）：不注入【顾客优惠券】块、无兜底表述——旧调用方零差异。
- 非空：注入"- 可用券: A、B"。
- 显式空列表 `[]`（或清理后为空）：注入兜底块"- 顾客暂无可用券；如提及权益请使用"会员专属优惠"表述"——对应 PRD"未选券则写会员专属优惠"。
- 校验：原始数组 ≤20（`Field(max_length=20)`），逐项清洗后剔除空白项，单项清理后 ≤100 字符（after validator，超限 422）。
- 微导购端约定：始终携带 `coupon_names`（无券传 `[]`），以稳定触发兜底。

- 备选：空与缺省同义——无法区分"顾客无券"（需兜底）与"旧调用方不关心券"（不应兜底），否。

### D6 会员字段：`customer.member_level` + `customer.points` 结构化新增

`member_level: Optional[str]`（自由文本清洗，进【顾客信息】"会员等级"行）；`points: Optional[int]`（非负，沿用 `max_length` 的 after validator 校验模式，负值 422）。`_CUSTOMER_FIELD_LABELS` 顺序：称呼、性别、年龄段、会员等级、积分、风格偏好、使用场景、尺码信息、预算、备注。不使用 `customer.extra` 兜底（审计需结构化查询）。

- 备选：经 `extra` 传入——零模型改动但契约松散、审计不可查询，否。
- 备注：PRD 原型"未购天数"未进文案，本次不加字段。

### D7 换一换：经 `extra_prompt` 自由文本承载，服务端零改动

不新增 `rewrite_reasons/rewrite_note`。调用方将修改原因（PRD 四选项多选）与补充说明拼接为文本放入 `extra_prompt`；服务端沿用【补充要求】块注入，`.sales_pitch` 既有"多轮会话规则"（对上一版反馈在原话术上调整）已覆盖改写语义。接口文档给出推荐拼接模板：

```
换一换修改要求：优惠/会员权益没体现；没引导到店/行动
补充说明：结尾再热情一点，提一下到店试穿
```

- 备选：新增结构化原因字段——本轮决策明确不引入（接口最小化），否；reasons 驱动固定提示词补充——依赖前端拼接质量，交由联调约定。

### D8 文本块顺序与格式

用户消息六块顺序：`【顾客信息】→【商品信息】→【促销活动】→【顾客优惠券】→【话术要求】→【补充要求】`（空块跳过、顺序保持）。新增块格式：

```
【促销活动】
- 万象城1000-200: 现在万象城有【部分整单满减】满1000减200，这套一起买刚好能用上。
- 沈阳2件88折: 两件一起买享 88 折，凑套更划算。（name 缺省时直接以 copy 成行）

【顾客优惠券】
- 可用券: 500元生日券、FUSION38元专属鞋券
（兜底时） - 顾客暂无可用券；如提及权益请使用"会员专属优惠"表述，不要罗列具体券名
```

【话术要求】块扩展行：`- 分享方式: 单品邀约（只介绍主款商品）` / `- 成套邀约（以主款为基点介绍整套搭配）`，位置在"风格"之后。活动/券块内不加约束语——禁止编造由提示词资产统一约束（现有"禁止编造活动、折扣"已覆盖，另补引用规则）。

### D9 实现要点（沿用现有模式）

- 校验清洗：复用 `_strip_html_tags/_strip_surrogates`；对象数组字段用「裸类型 + after validator」模式（与 `title`/`extra_prompt` 一致），`Field(max_length)` 只做原始长度防御。
- 服务层新增 `build_promotions_block(req)`、`build_requirements_block` 扩展、`build_coupons_block(req)`（缺省返回空串）。
- 审计 `input_block` 顶层新增：`share_mode`（无→null）、`promotions`（无或空→null，否则 `model_dump(exclude_none=True)` 列表）、`coupon_names`（无→null，显式[]→[]，记录清洗后值）；`member_level/points` 随 `customer.model_dump(exclude_none=True)` 自动纳入。
- 提示词资产：`.sales_pitch/skills/sales-pitch/SKILL.md` 增四语气写作定义（亲切自然/活力潮流/专业尊贵/简约高效）、单品/成套结构说明、活动与券引用规则；`AGENTS.md` 同步补充。

### D10 文档与联调约定

`.qoder/reference/api-spec.yaml` 与 `README.md` 更新：新字段定义（含上限）、`coupon_names` 缺省/空列表语义、换一换经 `extra_prompt` 的拼接模板、四语气取值清单。PRD 6.6 字段与本契约的对应关系随文档说明（memberId↔customer.union_id、productIds↔products[].sku_id、promoIds↔promotions[].promo_id、couponNames↔coupon_names、tone↔pitch_style、rewriteReasons/rewriteNote↔extra_prompt）。

## Risks / Trade-offs

- [同一会员多份文案（单品/成套/多个套）共享会话历史，可能出现轻量串扰] → 最新消息完整重述各块，模型以最新输入为准；如上线后观测到实质干扰，再评估可选 `copy_key` 会话隔离（不影响本次契约）。
- [换一换效果取决于调用方拼接文本质量] → 文档给出拼接模板与示例；`.sales_pitch` 多轮规则兜底改写语义；联调期抽查。
- [活动 `copy` 必填可能挡住不具备文案的接入方] → 有意设计：宁缺勿编；如 POS 侧无文案，后续再评估由调用方生成文案的替代方案。
- [单品/成套为提示词软约束] → 通过 e2e 生成抽查验证，必要时收紧 `.sales_pitch` 结构说明。
- [中文字段值（pitch_style）与自由描述并存] → 仅精确匹配四值走预设，其余透传，行为稳定可预期。

## Migration Plan

1. 本服务先发布（纯兼容扩展，无 DB DDL——审计 input 为 JSON 快照，新增键无需迁移）。
2. 发布后与微导购方联调：按其工作台调用节奏验证单品/成套、活动/券、换一换链路。
3. 回滚：回退镜像即可；新字段被旧版本 `extra="ignore"` 静默忽略，生成功能降级但不报错。

## Open Questions

- 换一换拼接模板的最终措辞（含 PRD 四原因的中文文案）随联调与对方页面文案对齐——服务端不受影响。
- `copy_key` 会话隔离是否需要有真实线上观察数据后再决定，不阻塞本次交付。
