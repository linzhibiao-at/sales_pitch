#!/bin/bash
# =============================================================================
# run_milvus_hybrid_migration.sh
# FILA Milvus hybrid(BM25+dense)检索复刻 descent —— 全流程上线脚本
#
# 流程：
#   1. preflight    环境自检（venv / ARK_API_KEY / Milvus cloud / ES / config）
#   2. catalog       全量重建 skus.jsonl（补 descent 字段：brand_line/year/features/...）
#   2b. select_images 填 tryon_image/index_images/display_image（ES 需，否则全被跳过→空索引）
#   3. es            --reset 重建 ES skus 索引（新 mapping + 富化 search_text）
#   4. milvus        --reset 建 fila_sku_hybrid_vectors 集合并灌数据（BM25 Function + IVF_FLAT）
#   5. smoke         内联 python 调 FilaSkuHybridSearcher.search_hybrid 验证可检索
#   6. deploy        git push + 提示服务器跑 restart.sh
#
# 用法（在 fila_outfit 目录）::
#   export ARK_API_KEY=...
#   bash scripts/run_milvus_hybrid_migration.sh
#
# 可选 flag（部分重跑）::
#   --skip-catalog / --skip-images / --skip-es / --skip-milvus / --skip-smoke / --no-push
#   --smoke-query "短袖T"      默认 smoke 查询词
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOGDIR="$ROOT/data/logs/migration"
mkdir -p "$LOGDIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="$LOGDIR/hybrid_migration_${STAMP}.log"

SMOKE_QUERY="短袖T"
SKIP_CATALOG=0; SKIP_IMAGES=0; SKIP_ES=0; SKIP_MILVUS=0; SKIP_SMOKE=0; NO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --skip-catalog) SKIP_CATALOG=1 ;;
    --skip-images)  SKIP_IMAGES=1 ;;
    --skip-es)      SKIP_ES=1 ;;
    --skip-milvus)  SKIP_MILVUS=1 ;;
    --skip-smoke)   SKIP_SMOKE=1 ;;
    --no-push)      NO_PUSH=1 ;;
    --smoke-query)  shift_next=1 ;;
    *)
      if [ "${shift_next:-0}" = "1" ]; then SMOKE_QUERY="$arg"; shift_next=0; fi ;;
  esac
done

# 颜色
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'

log()  { echo -e "${CYN}[$(date +%H:%M:%S)]${RST} $*" | tee -a "$LOG"; }
ok()   { echo -e "${GRN}[$(date +%H:%M:%S)] ✓${RST} $*" | tee -a "$LOG"; }
warn() { echo -e "${YLW}[$(date +%H:%M:%S)] !${RST} $*" | tee -a "$LOG"; }
die()  { echo -e "${RED}[$(date +%H:%M:%S)] ✗${RST} $*" | tee -a "$LOG" >&2; exit 1; }

run() { log "▶ $*"; "$@" 2>&1 | tee -a "$LOG"; }

# 检测 venv python（仓库根 .venv）
PYTHON_BIN="$(command -v python3 || command -v python)"
[ -x "/home/jovyan/fila_outfit/.venv/bin/python" ] && PYTHON_BIN="/home/jovyan/fila_outfit/.venv/bin/python"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

log "================ hybrid migration 开始 ================"
log "ROOT=$ROOT  PYTHON=$PYTHON_BIN  LOG=$LOG"

# -----------------------------------------------------------------------------
# 1. preflight
# -----------------------------------------------------------------------------
log "==== [1/6] preflight 环境自检 ===="

[ -n "${ARK_API_KEY:-}" ] || die "ARK_API_KEY 未设置（build_hybrid_index 的 embed_text 需要）"
ok "ARK_API_KEY 已设置"

"$PYTHON_BIN" - <<'PY' || die "pymilvus/依赖不可用"
import sys
sys.path.insert(0, ".")
import pymilvus
from pymilvus import Function, FunctionType, AnnSearchRequest, RRFRanker, WeightedRanker, MilvusClient
print("pymilvus", pymilvus.__version__, "BM25/hybrid API ok")
PY
ok "pymilvus BM25/hybrid API 可用"

"$PYTHON_BIN" - <<'PY' || die "Milvus 配置自检失败"
import sys; sys.path.insert(0, ".")
from backend.config import load_config, get_milvus_uri, is_milvus_lite_local_uri
cfg = load_config()
mv = cfg.get("milvus") or {}
assert mv.get("enabled"), "milvus.enabled=false"
uri = get_milvus_uri(cfg)
assert uri, "Milvus URI 为空：设 FILA_MILVUS_MODE=cloud 或 FILA_MILVUS_URI"
assert not is_milvus_lite_local_uri(uri), f"BM25 Function 不支持 Milvus Lite(*.db)，当前 uri={uri}；请用 cloud"
col = (mv.get("collections") or {}).get("sku_hybrid_vectors") or "fila_sku_hybrid_vectors"
print("milvus uri:", uri)
print("hybrid collection:", col)
print("embedding dim:", (cfg.get("embedding") or {}).get("dimensions") or 1024)
PY
ok "Milvus cloud 配置 OK（非 Lite）"

# -----------------------------------------------------------------------------
# 2. catalog
# -----------------------------------------------------------------------------
if [ "$SKIP_CATALOG" = "1" ]; then
  warn "跳过 catalog（--skip-catalog）"
else
  log "==== [2/6] catalog 全量重建 skus.jsonl（补 descent 字段）===="
  run "$PYTHON_BIN" scripts/build_catalog.py
  ok "catalog 重建完成"
fi

# -----------------------------------------------------------------------------
# 2b. select_images（填 tryon_image / index_images / display_image）
# -----------------------------------------------------------------------------
if [ "$SKIP_IMAGES" = "1" ]; then
  warn "跳过 select_images（--skip-images；ES 将跳过无图 SKU，若 skus.jsonl 已有图可跳）"
else
  log "==== [2b/6] select_images 填 tryon_image/index_images（ES 依赖，否则全跳过→空索引）===="
  run "$PYTHON_BIN" scripts/select_images.py
  ok "select_images 完成"
fi

# -----------------------------------------------------------------------------
# 3. ES
# -----------------------------------------------------------------------------
if [ "$SKIP_ES" = "1" ]; then
  warn "跳过 ES（--skip-es）"
else
  log "==== [3/6] ES skus 索引 --reset（新 mapping + 富化 search_text）===="
  run "$PYTHON_BIN" scripts/build_fila_es_index.py --reset --skip-outfits --no-verify
  ok "ES skus 索引重建完成"
fi

# -----------------------------------------------------------------------------
# 4. Milvus hybrid
# -----------------------------------------------------------------------------
if [ "$SKIP_MILVUS" = "1" ]; then
  warn "跳过 Milvus hybrid（--skip-milvus）"
else
  log "==== [4/6] Milvus fila_sku_hybrid_vectors --reset 建集合 + 灌数据 ===="
  run "$PYTHON_BIN" scripts/build_hybrid_index.py --reset
  ok "Milvus hybrid 集合构建完成"
fi

# -----------------------------------------------------------------------------
# 5. smoke
# -----------------------------------------------------------------------------
if [ "$SKIP_SMOKE" = "1" ]; then
  warn "跳过 smoke（--skip-smoke）"
else
  log "==== [5/6] smoke：FilaSkuHybridSearcher.search_hybrid 验证 ===="
  "$PYTHON_BIN" - <<PY | tee -a "$LOG" || die "smoke 验证失败"
import sys; sys.path.insert(0, ".")
from backend.retrieval.hybrid_search import FilaSkuHybridSearcher
s = FilaSkuHybridSearcher()
q = "${SMOKE_QUERY}"
print(f"hybrid_search(q={q!r}) ...")
items = s.search_hybrid(q, limit=5, output_fields=["sku_id","title","category_l2","brand_line"])
print(f"hits={len(items)}")
for i, h in enumerate(items, 1):
    print(f"  #{i} sku={h.get('sku_id')} score={h.get('score')} title={h.get('title')} cat={h.get('category_l2')} brand_line={h.get('brand_line')}")
assert items, "0 hits：检查集合是否有数据 / search_text 是否为空"
print("smoke 通过（hybrid 可检索）")
PY
  ok "smoke 通过（hybrid 可检索）"
fi

# -----------------------------------------------------------------------------
# 6. deploy
# -----------------------------------------------------------------------------
log "==== [6/6] deploy ===="
BRANCH="$(git branch --show-current)"
log "当前分支：$BRANCH"

if [ "$NO_PUSH" = "1" ]; then
  warn "跳过 git push（--no-push）"
else
  git push -u origin "$BRANCH" 2>&1 | tee -a "$LOG" || warn "git push 失败（可能需先 pull/rebase）"
  ok "已推送 $BRANCH"
fi

cat <<EOF | tee -a "$LOG"

${GRN}================ 迁移完成 ================${RST}
数据 / 索引已就绪。代码已推送至 ${BRANCH}。

下一步（在部署服务器上执行）：
  cd /home/jovyan/fila_outfit
  ./restart.sh        # git pull + 重启 uvicorn 8 workers

切量说明：
  recall_by_hybrid 已接入 outfit_recall，由 config recommend.text_recall_mode 开关控制：
    dense  = 旧 dense 文本向量通路（默认，行为不变）
    hybrid = BM25+dense hybrid 通路（hybrid 0 命中自动 fallback dense）
  切量：config.yaml 改 text_recall_mode: "hybrid" + restart；回滚：改回 "dense" + restart。

日志：$LOG
EOF

log "================ hybrid migration 结束 ================"
