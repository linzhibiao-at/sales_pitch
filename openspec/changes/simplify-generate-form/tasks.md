# 实施清单：精简「话术生成」输入（控制台表单 + 接口契约）

## 1. 前端表单结构

- [x] 1.1 `GenerateView.vue`：新增「导购信息」区块（导购工号必填输入，置顶于表单最上方）；从「顾客信息」卡片移除工号输入框 —— 验证：浏览器打开页面，最上方出现独立「导购信息」卡片，顾客信息内不再出现工号
- [x] 1.2 `GenerateView.vue`：顾客信息移除 尺码/身材、风格偏好、使用场景、预算范围、导购备注 控件 —— 验证：顾客信息仅剩 union_id、称呼、性别、年龄、会员等级、积分 6 个字段控件
- [x] 1.3 `GenerateView.vue`：商品信息移除 SKU ID、价格、类目、材质、卖点描述 控件（保留 商品名称 + 颜色）—— 验证：商品块仅显示 2 个字段控件；多件时第一件「主款」标记仍在
- [x] 1.4 `GenerateView.vue`：话术要求区移除 分享方式、触达渠道 控件 —— 验证：区块仅剩 话术风格、字数上限、补充要求 控件

## 2. 前端状态与请求构造

- [x] 2.1 `GenerateView.vue`：收缩表单状态（`form.customer` 仅保留 6 个键、`defaultProduct()` 收缩为 `{ title, color }`、删除顶层 `share_mode` / `channel`）—— 验证：`npm run build` 通过；文件内无 `size_info` / `style_preference` / `scene` / `budget` / `notes` / `sku_id` / `price` / `category` / `material` / `selling_points` / `share_mode` / `channel` 残留引用
- [x] 2.2 `GenerateView.vue`：精简 `doGenerate` 请求构造与请求快照（products 映射仅 title/color；payload 删除 share_mode/channel 条件展开）—— 验证：生成一次请求后，审计详情中 `input.customer` / `input.products` 不含被移除字段
- [x] 2.3 `GenerateView.vue` + `main.css`：删除 share_mode 摘要徽章与 `shareModeLabel` helper、商品徽章 key 简化为 `p.title`、移除无引用的 `.badge-purple` —— 验证：构建通过；请求摘要仅剩 风格/活动/券 徽章
- [x] 2.4 `AuditView.vue`：移除「分享方式」展示行与 `shareModeLabel` helper —— 验证：审计详情不再出现该行；`npm run build` 通过

## 3. 后端入参契约收敛

- [x] 3.1 `backend/models.py`：`SalesPitchCustomerInfo` 删除 size_info / style_preference / scene / budget / notes 字段及 `_sanitize_text_fields` 中的对应引用 —— 验证：模型无这 5 个字段；相关单测更新后通过
- [x] 3.2 `backend/models.py`：`SalesPitchProductInfo` 删除 sku_id / price / category / material / selling_points（含 `_strip_sku_id` 与 selling_points 清洗 validator）；`SalesPitchRequest` 删除 share_mode / channel（含 `_strip_text` 参数与 `Literal` 导入清理）—— 验证：携带旧字段的请求成功且字段被忽略（单测覆盖）
- [x] 3.3 `backend/services/sales_pitch_service.py`：`_CUSTOMER_FIELD_LABELS` 仅留 5 项、`_PRODUCT_FIELD_LABELS` 仅留 color、删除 `_SHARE_MODE_LABELS` / `_CHANNEL_LABELS` / `_fmt_price`；`build_products_block` 删除货号标注与价格行；`build_requirements_block` 删除分享方式行与渠道行；`generate()` 日志行去掉 channel；`_write_audit` 删除 share_mode 与 channel 键 —— 验证：生成请求的提示词无被移除字段行；审计 input 无 share_mode / channel
- [x] 3.4 `tests/test_sales_pitch.py`：改写涉及被移除字段的用例（share_mode 校验与行输出、价格格式化、顾客/商品全字段块、渠道映射、审计新入参中的 share_mode），新增兼容用例「携带全套旧字段的请求成功且提示词/审计无残留」—— 验证：`.venv/bin/python -m pytest tests/ -q` 全量通过

## 4. 提示词资产与文档同步

- [x] 4.1 `.sales_pitch/skills/sales-pitch/SKILL.md`：删除「分享方式与结构」「按渠道调整表达」内容；执行流程与话术撰写要求中相关表述（分享方式、渠道、风格偏好、使用场景）随字段收敛更新 —— 验证：文件内无上述过时规则残留
- [x] 4.2 `.sales_pitch/AGENTS.md`：删除「分享方式」工作原则；更新对已删字段的引用（风格偏好、使用场景、价格、材质）—— 验证：工作原则与当前字段集一致
- [x] 4.3 `.qoder/reference/api-spec.yaml`：三个 schema 删除 12 个字段定义及相关枚举 —— 验证：YAML 可解析、无被移除字段残留
- [x] 4.4 根 `README.md`：请求示例、字段说明与 PRD 映射表同步删除被移除字段 —— 验证：示例与字段表与接口实际一致
- [x] 4.5 `web/README.md`：更新 4.1「话术生成页」区块表与摘要徽章说明（删除分享方式/渠道）—— 验证：表格描述与页面实际字段一致
- [x] 4.6 `docs/design-doc/技术架构文档.md` §4.7：数据模型表修正为当前真实字段集（customer: union_id / nickname / gender / age / member_level / points / extra；product: title / color / extra；request: app_id / guide_num / customer / products[1-10] / promotions / coupon_names / pitch_style / max_length / extra_prompt）—— 验证：表格与 `models.py` 一致

## 5. 集成验证

- [x] 5.1 构建与静态检查：`cd web && npm run build` 成功、IDE 无报错 —— 验证：构建输出无错误
- [x] 5.2 全量单测：`.venv/bin/python -m pytest tests/ -q` —— 验证：全部通过
- [x] 5.3 端到端手测（对照 spec 场景）：① 浏览器生成页以最小字段集生成成功，请求体与审计无被移除字段；② 直接调用接口发送携带全套旧字段的请求 → 成功且话术/审计无残留；③ 工号回填、券「暂无可用券」空数组等既有场景回归 —— 验证：全部场景通过
- [x] 5.4 规格校验：`openspec validate "simplify-generate-form" --strict` 通过 —— 验证：无校验错误
