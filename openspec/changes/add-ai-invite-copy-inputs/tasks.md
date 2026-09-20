## 1. 模型层（backend/models.py + tests/test_sales_pitch.py）

- [x] 1.1 `SalesPitchCustomerInfo` 新增 `member_level`（自由文本清洗）与 `points`（非负整数校验）及中文标签映射，验证模型单测通过（携带注入、缺省不出现、负值 422）
- [x] 1.2 新增 `SalesPitchPromotionInfo`（`promo_id` strip 后非空、`copy` 清洗后非空、`name` 可选）与 `SalesPitchRequest.promotions`（上限 10），验证模型单测通过（合法携带、copy 空 422、超限 422、HTML 剥除）
- [x] 1.3 `SalesPitchRequest` 新增 `share_mode`（`Literal["item","set"]`，非法值 422）与 `coupon_names`（上限 20、清理后剔除空白项、单项清理后 ≤100、超限 422），验证模型单测通过

## 2. 服务层（backend/services/sales_pitch_service.py）

- [x] 2.1 新增 `build_promotions_block` 与 `build_coupons_block`（券：缺省返回空串、非空列券名、显式空列表输出"会员专属优惠"兜底指示），验证单测覆盖三种态与块格式
- [x] 2.2 扩展 `_PITCH_STYLE_LABELS` 四语气预设与 `build_requirements_block`（新增分享方式行），user_msg 按【顾客信息】→【商品信息】→【促销活动】→【顾客优惠券】→【话术要求】→【补充要求】拼装，验证单测断言块顺序、四语气映射、未知 pitch_style 透传、缺省零变化
- [x] 2.3 审计 `input_block` 新增 `share_mode`（无→null）、`promotions`（无或空→null）、`coupon_names`（无→null、显式[]→[]），`customer` 快照自动含 `member_level/points`，验证单测断言各态记录值

## 3. 提示词资产（.sales_pitch/）

- [x] 3.1 `skills/sales-pitch/SKILL.md` 增补四语气写作定义（亲切自然/活力潮流/专业尊贵/简约高效）、单品/成套结构说明、活动与券引用规则，`AGENTS.md` 同步，验证 grep 可检出四语气与结构段落

## 4. 文档（.qoder/reference/api-spec.yaml + README.md）

- [x] 4.1 `api-spec.yaml` 更新请求 schema（新字段、上限、coupon_names 缺省/空列表语义）与审计 input 描述，验证 YAML 可被解析且示例与实现一致
- [x] 4.2 `README.md` 更新请求体示例与字段说明、四语气取值清单、换一换经 `extra_prompt` 的推荐拼接模板、PRD 6.6 字段映射表，验证按示例构造的请求可真实调用成功

## 5. 集成验证

- [x] 5.1 全量回归 `pytest tests/ -q` 全部通过
- [x] 5.2 真实端到端（真实 LLM + 审计）：单品/成套结构符合预期；活动/券原文被引用；`coupon_names: []` 出现"会员专属优惠"；换一换（extra_prompt）按修改要求重写；审计 input 各态记录正确；无新字段的旧式请求提示词与审计与变更前一致
