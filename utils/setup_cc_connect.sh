#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== 1. 安装 cc-connect ==="
if ! command -v npm &> /dev/null; then
    echo "npm 未安装，请先安装 Node.js (建议 nvm + Node 20+)"
    exit 1
fi
npm install -g cc-connect

# 校验飞书环境变量
if [ -z "${FEISHU_CC_APP_ID:-}" ]; then
    echo "错误: 环境变量 FEISHU_CC_APP_ID 未设置"
    echo "请先 export FEISHU_CC_APP_ID=<your_app_id> 再运行本脚本"
    exit 1
fi
if [ -z "${FEISHU_CC_APP_SECRET:-}" ]; then
    echo "错误: 环变量 FEISHU_CC_APP_SECRET 未设置"
    echo "请先 export FEISHU_CC_APP_SECRET=<your_app_secret> 再运行本脚本"
    exit 1
fi
echo "FEISHU_CC_APP_ID 已设置: ${FEISHU_CC_APP_ID}"

echo ""
echo "=== 2. 写入 ~/.cc-connect/config.toml ==="
mkdir -p "$HOME/.cc-connect"

# 若已存在则先备份
if [ -f "$HOME/.cc-connect/config.toml" ]; then
    cp "$HOME/.cc-connect/config.toml" "$HOME/.cc-connect/config.toml.bak.$(date +%s)"
    echo "已备份原 config.toml"
fi

WORK_DIR="$(pwd)"
cat > "$HOME/.cc-connect/config.toml" <<EOF
# cc-connect configuration
# Docs: https://github.com/chenhg5/cc-connect

language = "en"

[log]
level = "info"

[[projects]]
name = "my-project"

[projects.agent]
type = "claudecode"   # "claudecode", "codex", "cursor", "gemini", "qoder", "opencode", or "iflow"

[projects.agent.options]
work_dir = "${WORK_DIR}"
mode = "default"
# model = "claude-sonnet-4-20250514"

# --- Choose at least one platform below ---

# Feishu / Lark (WebSocket, no public IP needed)

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "${FEISHU_CC_APP_ID}"
app_secret = "${FEISHU_CC_APP_SECRET}"
# For more platforms (DingTalk, Telegram, Slack, Discord, LINE, WeChat Work) see docs
EOF
echo "已写入 ~/.cc-connect/config.toml (work_dir=${WORK_DIR})"

echo ""
echo "=== 3. 启动 cc-connect ==="
# 先停掉旧进程，避免端口/WebSocket 冲突
OLD_PIDS=$(pgrep -f "cc-connect" || true)
if [ -n "$OLD_PIDS" ]; then
    echo "发现旧 cc-connect 进程: $OLD_PIDS，先停止"
    for pid in $OLD_PIDS; do
        kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
fi

nohup cc-connect > cc-connect.log 2>&1 &
echo "cc-connect 已启动, PID: $!"
echo "日志文件: $(pwd)/cc-connect.log"

echo ""
echo "=== 完成 ==="
echo "查看日志: tail -f $(pwd)/cc-connect.log"
echo "停止: pkill -f cc-connect"
