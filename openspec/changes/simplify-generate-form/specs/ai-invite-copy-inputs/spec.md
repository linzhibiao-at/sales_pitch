## REMOVED Requirements

### Requirement: share_mode 分享方式请求字段

**Reason**: 业务精简——分享方式不再由接口下发；该字段随 `SalesPitchRequest` 契约收敛移除（最终字段集合见 `sales-pitch-input-contract`），避免提示词与审计继续承载无用信息。

**Migration**: 调用方停用 `share_mode` 传参（残留传参将被忽略，请求正常处理、不报错）；需要单品/成套差异化结构时改用 `extra_prompt` 补充要求表达。

## MODIFIED Requirements

### Requirement: 新入参写入审计快照

审计文档 input 快照 SHALL 记录新入参：`promotions`（未携带或空数组时为 null，否则记录各项 promo_id/name/copy）、`coupon_names`（未携带时为 null，显式空列表记录为空数组）；`customer` 快照 SHALL 在携带时包含 `member_level` 与 `points`，未携带时不出现。

#### Scenario: 审计记录新入参

- **WHEN** 携带 promotions、coupon_names、member_level、points 的请求处理完成（成功或失败）
- **THEN** 审计 input 依次记录对应值（自由文本为清理后原文，活动记录 id/name/copy 结构）

#### Scenario: 未携带时记录 null 或不出现

- **WHEN** 请求未携带新字段
- **THEN** 审计 input 中 promotions/coupon_names 为 null，customer 快照不含 member_level 与 points
