## 1. 数据模型改造 (models.py)

- [x] 1.1 `SalesPitchCustomerInfo` 新增 `union_id: str` 必传字段，加入 `_sanitize_text_fields` validator 和空白剥离；运行 `python -c "from backend.models import SalesPitchCustomerInfo"` 验证导入无误
- [x] 1.2 `SalesPitchRequest` 删除 `session_id` 字段；将 `guide_id: Optional[str]` 重命名为 `guide_num: str`（必传，`Field(min_length=1)`）；将 `customer` 从 `Optional` 改为必传；更新 `_strip_text` validator 中的字段名；运行 `python -c "from backend.models import SalesPitchRequest; print(SalesPitchRequest.model_fields)"` 验证字段结构正确

## 2. 导购校验模块重命名 (guide_auth.py, config.py, config.yaml)

- [x] 2.1 `backend/guide_auth.py`：`verify_guide_identity` 参数从 `guide_id` 改为 `guide_num`；`GuideUserStore.get` 参数从 `guide_id` 改为 `guide_num`；错误信息中的 `guide_id` 更新为 `guide_num`；运行 `python -c "from backend.guide_auth import verify_guide_identity"` 验证导入无误
- [x] 2.2 `backend/config.py`：`get_guide_auth_config` 中解析用户列表时的 key 从 `guide_id` 改为 `guide_num`；运行 `python -c "from backend.config import get_guide_auth_config; print(get_guide_auth_config())"` 验证配置解析正确
- [x] 2.3 `config.yaml`：`guide_auth.users` 列表中每个条目的 `guide_id` key 改为 `guide_num`；确认 YAML 格式有效
- [x] 2.4 `backend/routers/sales_pitch.py`：`body.guide_id` 改为 `body.guide_num`；运行 `python -c "from backend.routers.sales_pitch import router"` 验证导入无误

## 3. Service 层 session_id 生成逻辑 (sales_pitch_service.py)

- [x] 3.1 `SalesPitchService.generate` 方法：删除 `session_id = (req.session_id or "").strip() or uuid.uuid4().hex` 逻辑，改为 `session_id = f"{req.guide_num}_{req.customer.union_id}"`（空白已在模型层剥离）；确保不再导入 `uuid` 模块（如无其他引用）；运行 `python -c "from backend.services.sales_pitch_service import SalesPitchService"` 验证导入无误
- [x] 3.2 `_write_audit` 方法：审计 input 块中 `"guide_id"` key 改为 `"guide_num"`，值从 `req.guide_id` 改为 `req.guide_num`；验证审计文档结构正确

## 4. 审计模块更新 (request_audit.py)

- [x] 4.1 `backend/services/request_audit.py`：检查 `build_sales_pitch_doc` 和 `slim_audit_row` 中是否有 `guide_id` 引用并更新为 `guide_num`（如适用）；运行 `python -c "from backend.services.request_audit import build_sales_pitch_doc"` 验证导入无误

## 5. 单元测试同步更新

- [x] 5.1 `tests/test_sales_pitch.py`：更新请求体 fixture（`guide_id` → `guide_num`，新增 `customer.union_id`，`customer` 必传）；验证 `session_id` 不再生成 UUID 而是 `{guide_num}_{union_id}` 格式；运行 `pytest tests/test_sales_pitch.py -v` 全部通过
- [x] 5.2 `tests/test_guide_auth.py`：更新所有 `guide_id` 引用为 `guide_num`；运行 `pytest tests/test_guide_auth.py -v` 全部通过
- [x] 5.3 `tests/test_request_audit.py`：更新审计文档构造中的字段引用；运行 `pytest tests/test_request_audit.py -v` 全部通过
- [x] 5.4 `tests/test_auth.py`：检查是否有 `guide_id`/`session_id` 引用需要更新；运行 `pytest tests/test_auth.py -v` 全部通过
- [x] 5.5 运行 `pytest tests/ -v` 确认全部测试通过

## 6. 前端适配

- [x] 6.1 `web/src/views/GenerateView.vue`：去掉 `currentSessionId` ref 和 `sp_session_id` localStorage 逻辑；去掉"新建会话"按钮或改为展示当前 session_id；顾客信息表单新增 `union_id` 输入框（必填）；请求 payload 中 `guide_id` 改为 `guide_num`，删除 `session_id` 字段，确保 `customer.union_id` 传入
- [x] 6.2 `web/src/api/index.js`：检查 `generatePitch` 返回类型注释，确认 `session_id` 仍在响应中（系统生成）；请求参数注释同步更新
- [x] 6.3 前端构建验证：`cd web && npm run build` 无报错

## 7. OpenSpec spec 同步

- [x] 7.1 更新 `openspec/specs/guide-identity/spec.md` 主 spec，将所有 `guide_id` 替换为 `guide_num`（与 delta spec 的 MODIFIED 内容一致），为后续 archive 合并做准备
