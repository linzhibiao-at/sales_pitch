#!/bin/bash

echo "=== 停止所有 tail -f 进程 ==="

# 查找所有 tail -f 进程（排除 grep 自身）
pids=$(pgrep -f "tail -f" || true)
if [ -z "$pids" ]; then
    echo "没有找到 tail -f 进程"
    exit 0
fi

echo "找到 tail -f 进程:"
ps -p $(echo "$pids" | tr '\n' ',') -o pid,ppid,command --no-headers 2>/dev/null || true
echo ""

for pid in $pids; do
    echo "终止进程: $pid"
    kill -9 "$pid" 2>/dev/null || true
done

echo ""
echo "验证: 检查是否还有残留 tail -f 进程..."
remaining=$(pgrep -f "tail -f" || true)
if [ -n "$remaining" ]; then
    echo "仍有残留进程: $remaining，强制清理..."
    echo "$remaining" | xargs kill -9 2>/dev/null || true
    echo "已强制清理"
else
    echo "所有 tail -f 进程已终止"
fi