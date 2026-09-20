## Context

话术生成的用户消息由三个文本块拼装：`【顾客信息】`（build_customer_block）、`【商品信息】`（build_products_block）、`【话术要求】`（build_requirements_block，含风格/渠道/字数）。请求模型 `SalesPitchRequest` 目前仅有 `pitch_style`/`channel`/`max_length` 三项结构化生成要求，无自由文本入口。参见 proposal.md - Why。

相关既有约束：

- 自由文本清洗：`models.py` 的 `_strip_html_tags`（防 LLM 内容过滤）+ `_strip_surrogates`（防 utf-8 编码失败）；`title` 的"裸 str + after validator"模式表明——带长度约束的 `Field` 会在清洗前拒绝含 surrogate 的文本，与剥除降级策略不一致
- 会话机制：`thread_id = {guide_num}_{union_id}` 固定不变，对应用户消息天然进入对话历史（session-management 能力）
- 审计快照：`input_block` 中自由字段采用 `(v or "").strip() or None` 记录模式（`pitch_style`/`channel` 同款）

## Goals / Non-Goals

**Goals:**
- `extra_prompt` 端到端落地：界面输入 → 请求契约 → 提示词注入 → 审计快照
- 与现有字段模式完全一致（清洗方式、空值过滤、审计记录），对老调用方向后兼容
- 导购自由输入以独立文本块注入，支持多轮场景下利用既有会话历史做调整

**Non-Goals:**
- 不做"仅本轮生效"的隔离机制——补充要求自然进入会话历史，充当多轮反馈调整入口
- 不设定补充要求与结构化要求（风格/渠道/字数）的优先级，由模型自行权衡
- 不改动 Agent 构建（SOUL/AGENTS.md 资源、middleware）与响应体结构

## Decisions

### D1: 字段名 `extra_prompt`，置于 `SalesPitchRequest` 顶层

**决策**: 新增可选字段 `extra_prompt`（`Optional[str]`），与 `pitch_style` 平级。

**理由**: 语义是"本次生成要求"而非顾客信息，与 `customer.notes`（顾客静态备注：关注点、历史消费）明确区分；`extra_prompt` 直白对应"额外提示词"，与 `extra` 自由扩展命名风格一致。

**备选方案**: 复用 `customer.notes` —— 顾客维度与生成要求混用，污染审计语义；`custom_requirement` —— 名称较长，且"要求"一词与 `pitch_style` 的自由描述能力重叠。

### D2: 独立文本块【补充要求】注入 user_msg 末尾

**决策**: 新增 `build_extra_prompt_block`，非空时以 `【补充要求】\n<原文>` 独立块拼入 `user_msg`，位置在【话术要求】之后。

**理由**: 补充要求可能多行、语义独立于结构化要求；指令靠近消息末尾更利于模型遵循；独立块在调试/日志中可辨识。

**备选方案**: 并入【话术要求】块追加一行 —— 块数更少但多行自由文本嵌在列表格式中可读性差。

### D3: 平级注入，不附加优先级措辞（已确认）

**决策**: 【补充要求】与【话术要求】平铺注入，提示词中不写"以本条为准"或"仅作参考"类措辞，冲突场景（如"简短干练" vs 输入"详细展开"）由模型自行权衡。

**理由**: 用户确认不设优先级；避免提示词过度设计。

**备选方案**: 补充要求优先（导购即时输入最贴近当下场景）；结构化字段优先（硬约束）。冲突场景的输出稳定性作为上线后观察点，如需调整仅改提示词措辞，不影响接口契约与 spec。

### D4: 500 字符上限，在 after validator 中校验清理后长度

**决策**: 长度上限 500 字符；采用"裸 `Optional[str]` 字段 + after validator"模式，在剥除 HTML 标签与 surrogate 之后检查 `len > 500` 并抛 `ValueError`（422）。

**理由**: 沿用 `title` 的既有模式——带约束的 `Field(max_length=...)` 在清洗前生效，含 surrogate 的超长文本会被先行拒绝，与剥除降级策略不一致。500 字符约为默认话术正文（150 字）的 3 倍以上，足以表达要求且可控。

**备选方案**: `Field(max_length=500)` —— 实现更短但与清洗顺序冲突（边缘场景行为不一致）。

### D5: 审计快照对齐既有记录模式

**决策**: `_write_audit` 的 `input_block` 新增 `"extra_prompt": (req.extra_prompt or "").strip() or None`。

**理由**: 与 `pitch_style`/`channel` 记录模式一致，空值统一为 `null`；无需截断（500 字符上限内）。

## Risks / Trade-offs

**[冲突场景输出不稳定]** → 无优先级措辞时，风格/字数与补充要求冲突的输出可能不符导购预期。Mitigation: 上线后观察，必要时仅调整提示词措辞（不改 spec 与接口契约）。

**[多轮会话累积]** → 补充要求进入对话历史，后续轮次即使不填写仍受其影响。Mitigation: 与既有 AGENTS.md"多轮反馈调整"设计一致，视为特性；若确需"仅本轮生效"须另立变更。

**[长度上限适应度]** → 500 字符可能过小限制导购表达、过大膨胀上下文。Mitigation: 按 500 先上线，后续如需要可做配置化（不在本次范围）。

## Migration Plan

1. 后端：模型字段 + 文本块构建 + user_msg 拼装 + 审计 input_block → 单元测试同步
2. 前端：话术要求卡片新增 textarea + payload 组装（空值不提交）
3. 契约文档同步（.qoder/reference/api-spec.yaml）
4. 部署无顺序依赖（可选字段追加，前后端可独立发布），无数据迁移
5. 回滚：移除字段引用即恢复原行为，历史审计数据不受影响
