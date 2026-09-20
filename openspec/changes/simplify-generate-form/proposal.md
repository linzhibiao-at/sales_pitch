# 精简「话术生成」输入：控制台表单与接口契约

## Why

Web 控制台「话术生成」页表单当前按后端接口能力全集铺开，但控制台的实际使用场景是微导购联调与导购试跑：仅核心字段有填写价值，其余选填字段拉低填写效率、分散注意力。同时「导购工号」被放在「顾客信息」卡片内，字段归属混淆（工号是导购身份标识，不属于顾客画像）。

后端接口同样承载业务已不再使用的入参字段（尺码 / 风格偏好 / 使用场景 / 预算 / 备注、货号 / 价格 / 类目 / 材质 / 卖点、分享方式 / 触达渠道）：字段进入提示词块与审计快照，增加校验、注入与存储的冗余面。微导购邀约工作台实际下发的核心字段集（工号 + 会员标识 / 会员等级 / 积分 + 商品名 + 优惠信息 + 语气 / 字数 / 补充要求）已稳定，接口契约应收敛到该核心集，与前端保持一致。

## What Changes

- 「导购工号 (guide_num)」从「顾客信息」卡片剥离，独立为「导购信息」区块并置于表单最上方；仍为必填，仍持久化到浏览器本地存储（`sp_guide_num`）
- 「顾客信息」移除 5 个字段控件：尺码 / 身材、风格偏好、使用场景、预算范围、导购备注；保留：会员标识 (union_id)、称呼、性别、年龄 / 年龄段、会员等级、积分
- 「商品信息」移除 5 个字段控件：SKU ID、价格（元）、类目、材质、卖点描述；保留：商品名称（必填）、颜色；1-10 件上限、第一件主款标记等既有行为不变
- 「话术要求」移除 2 个字段控件：分享方式、触达渠道；保留：话术风格、字数上限、补充要求
- 请求构造逻辑同步精简：表单不再发送被移除字段
- **后端接口契约同步收敛（BREAKING）**：`SalesPitchCustomerInfo` 移除 size_info / style_preference / scene / budget / notes；`SalesPitchProductInfo` 移除 sku_id / price / category / material / selling_points；`SalesPitchRequest` 移除 share_mode / channel
- 被移除字段若仍被旧调用方传入：请求正常处理且字段被忽略（沿用 Pydantic 默认 extra 语义，兼容降级、不报 422）
- 提示词文本块（【顾客信息】【商品信息】【话术要求】）与审计快照随动收敛，不再包含被移除字段；生成话术不再引用价格 / 类目 / 尺码 / 场景等信息
- 提示词资产（`SKILL.md` / `AGENTS.md`）移除分享方式与渠道写作规则及相关字段引用；`.qoder/reference/api-spec.yaml`、根 `README.md`、`docs/design-doc/技术架构文档.md` 数据模型表同步更新
- 「促销与优惠」区块保持不变
- 同步更新 `web/README.md` 页面说明

## Capabilities

### New Capabilities

- `web-generate-form`: Web 控制台「话术生成」页表单的区块构成与字段清单（导购信息独立区块；顾客信息 / 商品信息 / 话术要求保留字段集合；促销与优惠区块约束）
- `sales-pitch-input-contract`: `/v1/sales-pitch/generate` 入参字段契约（顾客 / 商品 / 请求三级字段集合；被移除字段的兼容降级；生成提示词与审计随动收敛）

### Modified Capabilities

- `ai-invite-copy-inputs`: 移除 `share_mode 分享方式请求字段` 需求（业务精简，分享方式不再由接口下发）；改写 `新入参写入审计快照` 需求（审计不再记录 share_mode）

## Impact

- 前端：`web/src/views/GenerateView.vue`（模板、表单状态、请求构造、请求摘要徽章、样式）、`web/src/views/AuditView.vue`（移除分享方式展示）、`web/README.md`
- 后端：`backend/models.py`（三级模型字段与 validator）、`backend/services/sales_pitch_service.py`（字段标签、文本块构建、审计快照、日志行）、`tests/test_sales_pitch.py`
- 资产与文档：`.sales_pitch/skills/sales-pitch/SKILL.md`、`.sales_pitch/AGENTS.md`、`.qoder/reference/api-spec.yaml`、根 `README.md`、`docs/design-doc/技术架构文档.md`
- **破坏性变更**：入参契约收窄；被移除字段传入不再进入提示词与审计（请求本身不报错）。审计历史数据与 PRD 文档不回溯修改
