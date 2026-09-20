## Purpose

为营销话术生成接口提供导购自由补充要求的端到端能力：导购在生成界面输入自然语言要求（如"突出秋冬新品""强调会员权益""再活泼一些"），系统将其作为提示词的一部分参与话术生成，并记录于审计快照。

## ADDED Requirements

### Requirement: extra_prompt 请求字段

`SalesPitchRequest` SHALL 包含可选字段 `extra_prompt`（字符类型，导购自由补充要求）；未携带、为空字符串或纯空白 SHALL 均视为未提供补充要求；字段长度 SHALL NOT 超过 500 个字符，超出 SHALL 被拒绝；字段为自由文本，SHALL 沿用接口现有的自由文本清洗策略（剥除 HTML 标签与 lone surrogate）后参与生成。

#### Scenario: 请求携带 extra_prompt

- **WHEN** 请求体携带 `"extra_prompt": "突出秋冬新品，提醒会员双倍积分"`
- **THEN** 请求正常处理，补充要求参与话术生成

#### Scenario: 请求未携带 extra_prompt

- **WHEN** 请求体不包含 `extra_prompt` 字段
- **THEN** 请求正常处理，行为与本次变更前一致（不注入补充要求）

#### Scenario: extra_prompt 为空白字符串视为未提供

- **WHEN** 请求体携带 `"extra_prompt": "   "`
- **THEN** 视为未提供补充要求，不注入生成提示词，行为与未携带一致

#### Scenario: extra_prompt 超过长度上限被拒绝

- **WHEN** 请求体携带长度超过 500 个字符的 `extra_prompt`
- **THEN** 接口返回 422（Pydantic 校验失败），指示字段超长

#### Scenario: extra_prompt 中的 HTML 标签被剥除

- **WHEN** 请求体携带的 `extra_prompt` 包含 HTML 标签（如 `<script>`）
- **THEN** 标签被剥除后参与生成，不因内容过滤导致话术生成失败

### Requirement: 补充要求注入生成提示词

系统 SHALL 将非空的 `extra_prompt`（去除首尾空白后）以独立文本块【补充要求】注入话术生成的用户消息，位置紧随【话术要求】块之后；空值 SHALL NOT 产生该文本块，其余文本块与既有行为一致；注入时 SHALL NOT 附加与结构化要求（风格/渠道/字数）的优先级声明，两者平级由模型自行权衡。

#### Scenario: 补充要求以独立块注入

- **WHEN** 请求携带非空 `extra_prompt`
- **THEN** 生成用的用户消息包含独立【补充要求】块，其内容为去除首尾空白后的原文，位于【话术要求】块之后

#### Scenario: 空值不注入

- **WHEN** 请求未携带 `extra_prompt` 或值为纯空白
- **THEN** 用户消息不包含【补充要求】块

#### Scenario: 与结构化要求平级注入

- **WHEN** 请求同时携带 `pitch_style`/`channel`/`max_length` 与非空 `extra_prompt`
- **THEN** 【话术要求】与【补充要求】两个块同时注入，提示词中不出现声明补充要求优先于或不优先于结构化要求的措辞

### Requirement: 补充要求写入审计快照

审计文档的 input 快照 SHALL 记录 `extra_prompt`：请求携带非空值时记录去除首尾空白后的原文，未提供或纯空白时记录 null。

#### Scenario: 审计记录补充要求

- **WHEN** 携带非空 `extra_prompt` 的请求处理完成（成功或失败）
- **THEN** 审计文档 `input.extra_prompt` 为去除首尾空白后的原文

#### Scenario: 未提供时审计记录 null

- **WHEN** 请求未携带 `extra_prompt` 或值为纯空白
- **THEN** 审计文档 `input.extra_prompt` 为 null

### Requirement: 导购界面补充要求输入

话术生成界面 SHALL 在话术要求区域提供补充要求自由文本输入入口（可选）；输入为空或纯空白时 SHALL NOT 将该字段随请求提交。

#### Scenario: 界面提供补充要求输入入口

- **WHEN** 导购打开话术生成页面
- **THEN** 话术要求区域展示补充要求输入入口（多行文本，带示例占位提示），与风格/渠道/字数控件同区

#### Scenario: 空输入不随请求提交

- **WHEN** 导购未填写补充要求，或仅输入空白字符后点击生成
- **THEN** 请求体不包含 `extra_prompt` 字段

#### Scenario: 填写后随请求提交

- **WHEN** 导购填写补充要求并点击生成
- **THEN** 请求体包含 `extra_prompt` 字段，值为输入内容去除首尾空白
