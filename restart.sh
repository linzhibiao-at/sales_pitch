#!/bin/bash
set -e

echo "=== 1. 停止所有 uvicorn 进程 ==="

kill_tree() {
    local ppid=$1
    local children
    children=$(pgrep -P "$ppid" 2>/dev/null || true)
    for child in $children; do
        kill_tree "$child"
    done
    if kill -0 "$ppid" 2>/dev/null; then
        kill -9 "$ppid" 2>/dev/null || true
        echo "  已终止进程: $ppid"
    fi
}

main_pids=$(pgrep -f "uvicorn backend.main:app" || true)

if [ -z "$main_pids" ]; then
    echo "没有找到 uvicorn 主进程"
else
    echo "找到 uvicorn 主进程: $main_pids"
    for pid in $main_pids; do
        echo "  清理进程树: $pid"
        kill_tree "$pid"
    done
fi

orphan_workers=$(pgrep -f "multiprocessing.spawn.*spawn_main" || true)
if [ -n "$orphan_workers" ]; then
    echo "清理残留的 multiprocessing worker: $orphan_workers"
    for pid in $orphan_workers; do
        kill -9 "$pid" 2>/dev/null || true
    done
fi

sleep 1

echo ""
echo "=== 2. Git Pull ==="
cd "$(dirname "$0")/.."
git pull

echo ""
echo "=== 3. 启动 uvicorn ==="
cd fila_agent_html
nohup uvicorn backend.main:app --host 0.0.0.0 --port 8080 --workers 8 > fila_agent_html.log 2>&1 &
echo "uvicorn 已启动, PID: $!"
echo "日志文件: $(pwd)/fila_agent_html.log"
