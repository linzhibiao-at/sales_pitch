## Purpose

定义 Web 控制台「话术生成」页表单的区块构成与字段清单：表单按「导购信息 → 顾客信息 → 商品信息 → 促销与优惠 → 话术要求」组织，仅暴露核心输入字段；字段集合与 `/v1/sales-pitch/generate` 接口契约保持一致（接口同步收敛到同一核心集）。

## ADDED Requirements

### Requirement: 导购信息独立区块

「话术生成」页 SHALL 在表单最上方提供「导购信息」区块，且该区块 SHALL 包含「导购工号 (guide_num)」必填输入框；导购工号 SHALL NOT 出现在「顾客信息」区块内。工号值 SHALL 在生成时写入浏览器本地存储（`sp_guide_num`），页面再次打开时 SHALL 自动回填。导购工号为空时 SHALL 禁止提交生成请求并给出提示。

#### Scenario: 导购工号位于独立区块

- **WHEN** 用户打开「话术生成」页
- **THEN** 表单最上方显示「导购信息」区块及其导购工号输入框；「顾客信息」区块内不再出现导购工号

#### Scenario: 导购工号持久化回填

- **WHEN** 用户填写导购工号并成功触发一次生成
- **THEN** 工号被写入本地存储；再次打开页面时输入框自动回填该值

#### Scenario: 导购工号为空时禁止提交

- **WHEN** 导购工号输入框为空
- **THEN** 「生成话术」按钮不可点击，并提示需要填写导购工号

### Requirement: 顾客信息字段集合

「顾客信息」区块 SHALL 提供以下字段控件：会员标识 (union_id，必填)、称呼、性别、年龄 / 年龄段、会员等级、积分；SHALL NOT 提供尺码 / 身材、风格偏好、使用场景、预算范围、导购备注控件。生成请求的 `customer` 对象 SHALL NOT 包含 size_info、style_preference、scene、budget、notes 字段。

#### Scenario: 顾客信息仅显示核心字段

- **WHEN** 用户查看「顾客信息」区块
- **THEN** 仅显示会员标识、称呼、性别、年龄、会员等级、积分 6 个字段控件

#### Scenario: 请求不携带已移除的顾客字段

- **WHEN** 用户提交生成请求
- **THEN** 请求体 `customer` 对象中不包含 size_info、style_preference、scene、budget、notes

### Requirement: 商品信息字段集合

「商品信息」区块每个商品 SHALL 提供：商品名称（必填）、颜色；SHALL NOT 提供 SKU ID、价格、类目、材质、卖点描述控件。生成请求的 `products` 各对象 SHALL NOT 包含 sku_id、price、category、material、selling_points 字段。商品 1-10 件上限、第一件主款标记、商品名称必填校验等既有行为 SHALL 保持不变。

#### Scenario: 商品信息仅显示核心字段

- **WHEN** 用户查看任一商品输入块
- **THEN** 仅显示商品名称与颜色 2 个字段控件

#### Scenario: 请求不携带已移除的商品字段

- **WHEN** 用户提交生成请求
- **THEN** 请求体 `products` 各对象中不包含 sku_id、price、category、material、selling_points

#### Scenario: 多件商品的主款标记保持

- **WHEN** 用户添加第 2 件商品
- **THEN** 第一个商品块显示「主款」标记（行为与本次变更前一致）

### Requirement: 话术要求字段集合

「话术要求」区块 SHALL 提供：话术风格（邀约工作台四档语气与兼容预设）、字数上限、补充要求；SHALL NOT 提供分享方式与触达渠道控件。生成请求 SHALL NOT 包含 share_mode 与 channel 字段。

#### Scenario: 话术要求不提供分享方式与触达渠道控件

- **WHEN** 用户查看「话术要求」区块
- **THEN** 仅显示话术风格、字数上限、补充要求控件，不显示分享方式与触达渠道

#### Scenario: 请求不携带分享方式与触达渠道

- **WHEN** 用户提交生成请求
- **THEN** 请求体不包含 share_mode 与 channel 字段

### Requirement: 促销与优惠区块

表单 SHALL 保留「促销与优惠」区块：门店活动动态列表（最多 10 条，活动 ID 与活动文案均填写时该行才随请求提交）与顾客优惠券控件（券名列表文本，逗号或换行分隔，最多 20 张；勾选「暂无可用券」时请求携带显式空数组以触发「会员专属优惠」兜底表述；留空且未勾选时不发送 coupon_names 字段）。

#### Scenario: 活动行不完整时不提交

- **WHEN** 某活动行仅填写了活动 ID 或仅填写了活动文案
- **THEN** 该行显示提示且不随请求提交；其余完整行正常提交

#### Scenario: 勾选顾客暂无可用券

- **WHEN** 用户勾选「顾客暂无可用券」并提交生成
- **THEN** 请求体 `coupon_names` 为显式空数组（`[]`）

#### Scenario: 优惠券留空且未勾选

- **WHEN** 优惠券文本框为空且未勾选「暂无可用券」并提交生成
- **THEN** 请求体不包含 `coupon_names` 字段
