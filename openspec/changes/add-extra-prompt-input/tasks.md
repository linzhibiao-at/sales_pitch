## 1. 后端模型（extra_prompt 字段）

- [x] 1.1 `backend/models.py`：`SalesPitchRequest` 新增可选字段 `extra_prompt`（裸 `Optional[str]`，不用带约束的 `Field`），after validator 依次完成 strip → 剥 HTML 标签 / lone surrogate → 清理后长度 > 500 抛 `ValueError`；验证：新增模型单测通过（纯空白视为未提供、501 字符拒绝、500 字符通过、HTML 标签与 surrogate 剥除）

## 2. 后端服务（提示词注入与审计）

- [x] 2.1 `backend/services/sales_pitch_service.py`：新增纯函数 `build_extra_prompt_block`（非空返回 `【补充要求】\n<strip 后原文>`，空值返回 `""`）；验证：新增块构建单测通过（单行、多行文本、空值、纯空白）
- [x] 2.2 `generate()` 中 `user_msg` 拼装追加该块（位于【话术要求】之后），空块过滤逻辑保持不变；验证：服务层单测断言 user_msg 含【补充要求】块且在【话术要求】之后；未携带时不产生该块且既有断言不变
- [x] 2.3 `_write_audit` 的 `input_block` 新增 `"extra_prompt": (req.extra_prompt or "").strip() or None`；验证：单测断言审计 `input.extra_prompt` 记录 strip 后原文、未提供时为 null

## 3. 前端（补充要求输入框）

- [ ] 3.1 `web/src/views/GenerateView.vue`：`form` 增加 `extra_prompt: ''`；话术要求卡片底部新增整行 textarea（label「补充要求（可选）」，placeholder 示例"如：突出秋冬新品／在上一版基础上再活泼一些"）；验证：`cd web && npm run build` 通过，页面渲染可见且可编辑
- [ ] 3.2 同文件 `doGenerate()`：payload 组装仅当 strip 后非空才携带 `extra_prompt`；验证：浏览器手测——空输入提交的请求体不含该字段（DevTools Network 面板），填写后包含且值为去除首尾空白的内容

## 4. 契约与文档同步

- [x] 4.1 `.qoder/reference/api-spec.yaml`：`SalesPitchRequest` 增加 `extra_prompt` 属性定义（string，≤500 字符，导购自由补充要求），`AuditDetail.input` 描述同步；验证：文档定义与模型实现逐项核对一致
- [x] 4.2 `README.md` 4.1 节：请求体示例与字段说明补充 `extra_prompt`（仅更新与新字段相关行，不处理其他滞后内容）；验证：示例字段与 api-spec.yaml 一致

## 5. 集成验证

- [ ] 5.1 全量回归：`.venv/bin/python -m pytest tests/ -q` 全绿（既有用例覆盖"未携带 extra_prompt"路径，证明行为不变）
- [ ] 5.2 端到端手测：本地启动后端与前端，填写补充要求生成话术（观察话术体现要求），通过审计查询接口核对 `input.extra_prompt` 记录；再发起一次不带补充要求的请求，确认行为与变更前一致
