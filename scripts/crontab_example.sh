#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# FILA 每日增量更新 — crontab 示例
#
# 将此文件内容加入 crontab（crontab -e）：
#   crontab -e
#   # 粘贴下方内容（按实际路径修改 /path/to/fila_agent_html）
#
# 环境变量须提前配置（推荐写入 ~/.bashrc 或单独 env 文件）：
#   export ARK_API_KEY=...
#   export ES_USERNAME=...
#   export ES_PASSWORD='...'
#   export FILA_MILVUS_URI=...
#   export FILA_MILVUS_USERNAME=...
#   export FILA_MILVUS_PASSWORD=...
#   export HIVE_USERNAME=...
#   export HIVE_PASSWORD='...'
#   export ANTA_LLM_API_KEY=...
# ──────────────────────────────────────────────────────────────────────────

# 项目根目录（按实际部署路径修改）
PROJECT_DIR=/path/to/fila_agent_html

# 虚拟环境 Python（若项目下有 .venv 则使用，否则使用系统 python3）
PYTHON=${PROJECT_DIR}/.venv/bin/python3
[ -x "$PYTHON" ] || PYTHON=python3

# 日志目录
LOG_DIR=${PROJECT_DIR}/data/logs
mkdir -p "$LOG_DIR"

# ── 周一至周六 03:00：增量更新（跳过搭配构建，节省时间）──────────────────
0 3 * * 1-6 cd ${PROJECT_DIR} && ${PYTHON} scripts/daily_incremental_update.py --env prod --skip-outfits >> ${LOG_DIR}/daily_cron.log 2>&1

# ── 每周日 03:00：全量重建（含搭配，确保数据完整性）───────────────────────
0 3 * * 0   cd ${PROJECT_DIR} && ${PYTHON} scripts/daily_incremental_update.py --full --env prod >> ${LOG_DIR}/daily_cron.log 2>&1

# ── 手动执行示例 ──────────────────────────────────────────────────────────
# # 增量（CSV 已就绪，跳过下载）
# cd /path/to/fila_agent_html && python3 scripts/daily_incremental_update.py --skip-download
#
# # 全量（含 Hive 下载 + 搭配）
# cd /path/to/fila_agent_html && python3 scripts/daily_incremental_update.py --full --env prod
#
# # 试运行（不实际执行）
# cd /path/to/fila_agent_html && python3 scripts/daily_incremental_update.py --dry-run --env prod
