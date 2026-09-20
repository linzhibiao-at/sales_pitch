# add-ai-invite-copy-inputs

## Why

微导购端「AI邀约」需求（`docs/prd/AI邀约链路（AI文案）-需求说明.html`）的工作台需要在单品/成套两种分享方式下实时生成邀约文案，入参包含分享方式、已选门店活动、已核验顾客券、会员等级/积分，并要求四档文案语气。当前话术生成接口缺少这些入参：无分享方式（单品/成套结构无法区分）、无活动/券注入（文案带不出促销与权益）、`customer` 无会员等级/积分、`pitch_style` 预设也不含"活力潮流"等语气。PRD「待确认事项」明确"AI 文案接口的正式路径与入参字段名待补充"——本变更将该接口入参补齐，与 PRD 6.6 节语义逐项对齐。

## What Changes

- 新增可选字段 `share_mode`（`item`/`set`）：标记单品或成套邀约文案；缺省时保持现有通用多商品行为；`products[0]` 约定为主款。
- 新增可选字段 `promotions`（对象数组 `{promo_id, name, copy}`）：注入已选门店 POS 活动；文案段引用传入 `copy` 原文，不编造活动。
- 新增可选字段 `coupon_names`（字符串数组）：注入已核验顾客券；显式空列表时按 PRD 兜底提示"会员专属优惠"。
- `customer` 新增可选结构化字段 `member_level`、`points`：支撑权益类文案（"您是金卡会员，积分还有 1260"）。
- 扩展 `pitch_style` 预设：支持四档语气（亲切自然/活力潮流/专业尊贵/简约高效）；现有三档英文预设与自由描述继续兼容；不新增 `tone` 字段。
- 换一换（文案修改要求）不新增结构化字段：调用方将修改原因与补充说明拼接为文本，经现有 `extra_prompt` 传入；接口文档给出推荐拼接约定。
- 审计 input 快照同步记录新字段；`api-spec.yaml`、README 与提示词资产（四语气写作定义、单品/成套结构、活动/券段落规则）同步更新。
- 无破坏性变更：所有新字段可选，现有调用方行为不变。

## Capabilities

### New Capabilities

- `ai-invite-copy-inputs`: AI邀约场景下的话术生成入参契约与生成行为——`share_mode`（单品/成套）、`promotions`（已选活动）、`coupon_names`（已选券及兜底）、`customer` 会员等级/积分、`pitch_style` 四语气预设。

### Modified Capabilities

（无。现有主 spec 中不涉及话术生成入参能力的变更。）

## Impact

- **接口**：`POST /v1/sales-pitch/generate` 请求体兼容性扩展（全部新增字段可选）；响应结构不变。
- **代码**：`backend/models.py`（新字段与校验）、`backend/services/sales_pitch_service.py`（新增【促销活动】【顾客优惠券】文本块、`share_mode` 结构指引、`pitch_style` 预设映射）、审计 `input_block`。
- **提示词资产**：`.sales_pitch/AGENTS.md`、`.sales_pitch/skills/sales-pitch/SKILL.md`。
- **文档**：`.qoder/reference/api-spec.yaml`、`README.md`；建议向微导购方输出拼接约定（换一换经 `extra_prompt`）。
- **测试**：`tests/test_sales_pitch.py` 扩展。
- **不在范围**：PRD 6.5 AI 搭配接口、6.7 发送接口（非本服务）；web 控制台 UI 不随本变更调整。
