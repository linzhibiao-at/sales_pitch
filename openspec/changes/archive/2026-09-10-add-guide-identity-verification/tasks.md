# 实施任务：导购身份校验（mock 数据源）

> 依赖顺序：配置/模型 → 校验组件 → 路由与审计接线 → 回归。测试风格沿用 `tests/` 现有 unittest + `unittest.mock.patch` 模式。

## 1. 配置与请求模型

- [x] 1.1 `backend/config.py` 新增 `get_guide_auth_config()`（风格对齐 `get_auth_config()`）：读取 `guide_auth` 段并归一化返回 `{enabled: bool, users: [{guide_id, ...}]}`——`guide_id` 剥空白、空 `guide_id` 条目丢弃、`guide_id` 缺失/非 dict 条目丢弃、段缺失时 `enabled=False`（向后兼容）；验证：`tests/test_guide_auth.py` 中配置读取单测通过（段缺失默认、strip 归一化、脏条目丢弃）
- [x] 1.2 `config.yaml` 新增 `guide_auth` 段：`enabled: true` + mock 用户 `G001`/`G002`（带 `name` 字段），注释说明"暂代 user 表、生产可关闭"；验证：`get_guide_auth_config()` 冒烟返回 `enabled=True` 且含两个用户
- [x] 1.3 `backend/models.py` 的 `SalesPitchRequest` 新增 `guide_id: Optional[str] = None`（注释：导购身份标识，校验开关见 `guide_auth`），并把 `guide_id` 加入既有 `_strip_text` 校验器列表；验证：模型单测通过——不传 `guide_id` 不报错（向后兼容）、`" G001 "` 归一化为 `"G001"`

## 2. 导购校验组件

- [x] 2.1 新建 `backend/guide_auth.py`：`GuideUserStore.get(guide_id) -> dict | None` 存在性查询（每次经 `get_guide_auth_config()` 读配置，不初始化缓存，实现热加载）+ 进程级单例与 getter（对齐 `auth.py` 风格）；验证：`get()` 命中/未命中/空列表单测通过
- [x] 2.2 `backend/guide_auth.py` 新增 `verify_guide_identity(guide_id) -> dict | None`：开关关闭返回 `None` 放行；启用时 `guide_id` 缺失/空白 → `HTTPException(400, "guide_id required")`，未知 → `HTTPException(401, "guide not found")`，命中返回用户 dict；验证：单测覆盖关闭放行/缺失 400/空白 400/未知 401/命中返回用户五个分支
- [x] 2.3 `tests/test_guide_auth.py` 建立组件单测（unittest 风格，patch `backend.guide_auth.get_guide_auth_config` 注入配置）；验证：`.venv/bin/python -m pytest tests/test_guide_auth.py -q` 全绿

## 3. 路由与审计接线

- [x] 3.1 `backend/routers/sales_pitch.py`：在 `app_id` 三层校验之后、调用 `_pitch_svc.generate` 之前调用 `verify_guide_identity(body.guide_id)`，命中时写入 `request.state.guide`（对齐 `request.state.caller` 惯例）；验证：代码走查确认调用点位于 app_id 校验后（应用级 → 用户级顺序，见 design D1）
- [x] 3.2 `backend/services/sales_pitch_service.py` 的 `_write_audit()`：`input_block` 增加 `"guide_id": (req.guide_id or "").strip() or None`；验证：`tests/test_sales_pitch.py` 补断言审计 `input.guide_id` 记录正确（传入时记录、未传为 None）
- [x] 3.3 `tests/test_guide_auth.py` 路由级集成测试（mini app + TestClient，模式仿 `TestVerifyApiKeyEndpoint`）：`Depends(verify_api_key)` + handler 内 `verify_guide_identity` 组合——无效 API Key + 合法 guide_id → 401 `invalid API key`（应用级优先）；启用 + 未知 guide_id → 401 `guide not found`；启用 + 缺失 → 400；关闭开关 + 无 guide_id → 200；验证：该组集成测试通过

## 4. 回归与收尾

- [x] 4.1 全量回归：`.venv/bin/python -m pytest tests/ -q` 全部通过（含既有 `test_auth.py`/`test_sales_pitch.py`/`test_request_audit.py` 无回归）
- [ ] 4.2 手工冒烟（可选，需本地 Redis/LLM 环境）：`uvicorn backend.main:app` 启动后，`POST /v1/sales-pitch/generate` 分别携带 `guide_id=G001`（通过）与 `guide_id=G999`（401 envelope 含 trace_id），确认统一错误出参与 `X-Trace-Id` 响应头
