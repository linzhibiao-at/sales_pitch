---
last_updated: 2026-09-01
status: active
owner: @linzhibiao
---

# 机械守护规则（Guardrails）

> 守护规则以**仓库根目录可执行命令**落地，不依赖额外工具链；CI 接入见 `../plans/backlog.md`。
>
> 分级：**硬门禁**（hard gate，任一失败禁止提交）与**建议级**（advisory，只提醒不拦截）。历史存量违规走**棘轮（ratchet）基线登记**：允许保持现状、禁止恶化、修完即删登记。

## 一键检查

在仓库根目录执行（bash / zsh 均可）：

```bash
fail=0

# GR-01 语法编译
.venv/bin/python -m compileall -q backend/ || { echo "❌ GR-01 compileall"; fail=1; }

# GR-02 测试基线（≥88，全绿）
out=$(.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3); echo "$out" | tail -1
echo "$out" | grep -qE '[0-9]+ (failed|error)' && { echo "❌ GR-02 存在失败用例"; fail=1; }
n=$(echo "$out" | tail -1 | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' | head -1)
if [ -z "$n" ] || [ "$n" -lt 88 ]; then echo "❌ GR-02 用例数 ${n:-0} < 基线 88"; fail=1; fi

# GR-03 输出禁令（print / traceback.print_exc）
grep -rnE '(^|[^a-zA-Z_.])print\(' backend --include='*.py' && { echo "❌ GR-03 print()"; fail=1; }
grep -rn 'traceback\.print_exc' backend --include='*.py' && { echo "❌ GR-03 traceback.print_exc"; fail=1; }

# GR-04 文件行数 ≤ 300
for f in $(find backend -name '*.py'); do
  lines=$(wc -l < "$f" | tr -d ' ')
  [ "$lines" -gt 300 ] && { echo "❌ GR-04 $f ${lines}行 > 300"; fail=1; }
done

# GR-05 分层依赖（仅模块级 import；见下方"装配根例外"）
grep -nE '^(from|import) backend\.(agent|llm|infra)' backend/routers/*.py backend/main.py && { echo "❌ GR-05 routers/main 越层"; fail=1; }
grep -nE '^(from|import) backend\.(routers|main)' backend/services/*.py && { echo "❌ GR-05 services 越层"; fail=1; }
grep -nE '^(from|import) backend\.(services|agent|llm|routers|main)' backend/infra/*.py && { echo "❌ GR-05 infra 越层"; fail=1; }
grep -nE '^(from|import) backend\.(services|agent|infra|routers|main)' backend/llm/*.py && { echo "❌ GR-05 llm 越层"; fail=1; }
grep -nE '^(from|import) backend\.(services|routers|main)' backend/agent/*.py && { echo "❌ GR-05 agent 越层"; fail=1; }

# GR-06 函数有效行数 ≤ 50（棘轮白名单见登记表）
.venv/bin/python - <<'PYEOF' || { echo "❌ GR-06 函数超长（或白名单恶化）"; fail=1; }
import ast, pathlib, sys
LIMIT = 50
BASELINE = {"backend/services/sales_pitch_service.py::generate": 60}

def eff_len(node, lines):
    doc = set()
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        d = node.body[0]
        doc = set(range(d.lineno, d.end_lineno + 1))
    n = 0
    for i in range(node.lineno, node.end_lineno + 1):
        if i in doc:
            continue
        t = lines[i - 1].strip()
        if not t or t.startswith('#'):
            continue
        n += 1
    return n

bad = []
for p in sorted(pathlib.Path('backend').rglob('*.py')):
    src = p.read_text(encoding='utf-8').splitlines()
    for node in ast.walk(ast.parse('\n'.join(src))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            L = eff_len(node, src)
            if L > LIMIT and BASELINE.get(f"{p}::{node.name}", 0) < L:
                bad.append(f"{p}::{node.name} 有效行={L} (上限{LIMIT})")
for b in bad:
    print("  ", b)
sys.exit(1 if bad else 0)
PYEOF

# GR-07 硬编码密钥扫描（代码文件；配置文件问题见 backlog 安全项）
grep -rnE 'sk-[0-9a-zA-Z]{16,}' backend tests --include='*.py' && { echo "❌ GR-07 硬编码密钥"; fail=1; }

# GR-08 future annotations 全覆盖（空 __init__.py 豁免）
missing=$(grep -rL '^from __future__ import annotations' backend --include='*.py' | grep -v '/__init__\.py$')
[ -n "$missing" ] && { echo "❌ GR-08 缺失: $missing"; fail=1; }

[ "$fail" -eq 0 ] && echo "✅ ALL GUARDRAILS PASSED" || { echo "GUARDRAILS FAILED"; exit 1; }
```

## 硬门禁明细

| 编号 | 规则 | 依据 |
|---|---|---|
| GR-01 | `backend/` 全量语法编译通过 | AGENTS.md 硬性规则 |
| GR-02 | pytest 全绿且用例数 ≥ 88 | AGENTS.md 规则 8 |
| GR-03 | 禁止 `print()` / `traceback.print_exc()` | AGENTS.md 规则 2 |
| GR-04 | `backend/` 单文件 ≤ 300 行 | AGENTS.md 规则 3 |
| GR-05 | 模块级 import 分层单向 | AGENTS.md 规则 1 |
| GR-06 | 函数有效行数 ≤ 50（棘轮） | AGENTS.md 规则 3 |
| GR-07 | 代码文件无硬编码密钥 | AGENTS.md 规则 9 |
| GR-08 | 每个模块 `from __future__ import annotations` | conventions/README.md |

各条单查命令（grep 类命中即违规，期望无输出）：

```bash
.venv/bin/python -m compileall -q backend/                        # GR-01
.venv/bin/python -m pytest tests/ -q                              # GR-02
grep -rnE '(^|[^a-zA-Z_.])print\(' backend --include='*.py'       # GR-03
find backend -name '*.py' -exec wc -l {} + | sort -rn | head -3   # GR-04（人工核对 ≤300）
grep -nE '^(from|import) backend\.(agent|llm|infra)' backend/routers/*.py backend/main.py  # GR-05
grep -rnE 'sk-[0-9a-zA-Z]{16,}' backend tests --include='*.py'    # GR-07
grep -rL '^from __future__ import annotations' backend --include='*.py' | grep -v __init__ # GR-08
```

### GR-05 装配根例外

`routers/sales_pitch.py::_init_agent_stack()` 在**函数体内**延迟 import `infra/llm/agent` 完成 Agent 栈装配——这是 `architecture/boundaries.md` 认可的组合根（composition root）模式。GR-05 用 `^(from|import)` 锚定**模块级（列 0）** import，因此不误伤该例外；但业务代码**禁止模仿**此模式绕过分层（新代码越层一律先改设计再写码）。

### GR-06 有效行数口径

AST 解析 `def` / `async def`，统计 `lineno`~`end_lineno` 内**非 docstring、非纯注释、非空行**的行数；嵌套函数归各自计数（不重复累计）。

## 棘轮基线登记表

修完某项后必须**同步删除登记**（收紧基线，防止回潮）：

| 登记项 | 当前值 | 门禁上限 | 处置计划 |
|---|---|---|---|
| `backend/services/sales_pitch_service.py::generate` | 60 有效行 | 50 | 拆分 prompt 构建/结果提取为独立函数（plans/backlog.md 架构治理） |
| pytest 用例基线 | 88 | 只增不减 | — |

## 建议级检查（advisory，不拦截）

```bash
# A-01 文档新鲜度：.qoder 文档超 60 天未更新则提醒
# 注：未提交/未跟踪的新文件 git log 无输出且退出码为 0，须用 ${:-} 兜底而非 ||
find .qoder -name '*.md' | while read f; do
  last=$(git log -1 --format=%ct -- "$f" 2>/dev/null)
  last=${last:-$(date +%s)}
  age=$(( ($(date +%s) - last) / 86400 ))
  [ "$age" -gt 60 ] && echo "⚠️ $f 已 ${age} 天未更新"
done

# A-02 敏感文件 Git 跟踪：当前已知 config/api_keys.yaml 被跟踪（backlog 安全项）
git ls-files config/api_keys.yaml && echo "⚠️ api_keys.yaml 在 Git 跟踪中（应为「无输出」）"
```

## CI 接入（待办）

GitHub Actions 工作流尚未创建。落地时在 `.github/workflows/guardrails.yml` 中以 `python 3.12 + pip install -r requirements.txt` 复跑上述一键脚本（pull_request 触发）；任务登记在 `../plans/backlog.md`。
