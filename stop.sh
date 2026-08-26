#!/bin/bash

echo "=== 停止所有 uvicorn 进程及其子进程 ==="

# 查找所有 uvicorn 进程
pids=$(pgrep -f uvicorn || true)
if [ -z "$pids" ]; then
    echo "没有找到 uvicorn 进程"
    exit 0
fi

echo "找到 uvicorn 进程:"
ps -p $(echo "$pids" | tr '\n' ',') -o pid,ppid,command --no-headers 2>/dev/null || true
echo ""

for pid in $pids; do
    # 获取该进程的所有子进程
    children=$(pgrep -P "$pid" || true)
    if [ -n "$children" ]; then
        echo "先终止进程 $pid 的子进程: $children"
        echo "$children" | xargs kill -9 2>/dev/null || true
    fi
    echo "终止主进程: $pid"
    kill -9 "$pid" 2>/dev/null || true
done

echo ""
echo "验证: 检查是否还有残留 uvicorn 进程..."
remaining=$(pgrep -f uvicorn || true)
if [ -n "$remaining" ]; then
    echo "仍有残留进程: $remaining，强制清理..."
    echo "$remaining" | xargs kill -9 2>/dev/null || true
    echo "已强制清理"
else
    echo "所有 uvicorn 进程已终止"
fi
