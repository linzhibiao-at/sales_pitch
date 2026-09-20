## Why

导购在生成话术前经常有个性化诉求（如"突出秋冬新品""强调会员双倍积分""在上一版基础上再活泼一些"），现有入参仅支持结构化的风格/渠道/字数三项，无法表达自由的自然语言要求。需要为导购提供一个自由输入框，其内容作为提示词的一部分参与话术生成，并借助既有的同导购同会员会话机制，天然支持多轮调整诉求。

## What Changes

- 前端"话术生成"页在「话术要求」卡片新增「补充要求（可选）」多行输入框，空值不随请求提交
- `SalesPitchRequest` 新增可选字段 `extra_prompt`（字符类型，上限 500 字，沿用现有 HTML 标签 / lone surrogate 清洗模式）
- prompt 组装新增独立文本块 `【补充要求】`，追加至 user_msg 末尾；空值不注入；**不设**与结构化要求的优先级，由模型自行权衡
- 审计 input 快照新增 `extra_prompt` 记录；`.qoder/reference/api-spec.yaml` 契约同步
- 多轮会话中补充要求自然进入对话历史累积（与现有 session 机制一致），不新增"仅本轮生效"的隔离机制

## Capabilities

### New Capabilities
- `pitch-extra-prompt`: 导购自由补充要求输入的端到端行为——请求字段契约（可选、清洗、长度上限）、prompt 注入规则（独立块、空值不注入、无优先级措辞）、审计快照记录

### Modified Capabilities
<!-- 无：session-management 与 guide-identity 的需求不变；话术生成字段此前未 spec 化，不构成对现有能力的修改 -->

## Impact

- **对外 API（向后兼容，无 BREAKING）**: `POST /v1/sales-pitch/generate` 请求体新增可选字段 `extra_prompt`，老调用方不受影响
- **后端模型**: `backend/models.py` — `SalesPitchRequest` 新增字段与校验
- **后端服务**: `backend/services/sales_pitch_service.py` — 新增 `【补充要求】` 文本块构建函数、user_msg 拼装、审计 input_block 字段
- **前端**: `web/src/views/GenerateView.vue` — 话术要求卡片新增 textarea 与 payload 组装
- **契约文档**: `.qoder/reference/api-spec.yaml` — `SalesPitchRequest`、`AuditDetail.input` 描述同步
- **测试**: `tests/test_sales_pitch.py` 新增字段注入、空值过滤、长度校验、审计记录用例
