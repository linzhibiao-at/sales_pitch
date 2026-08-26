#!/bin/bash
# 杀掉所有 claude 进程
pids=$(pgrep -f '^claude ' 2>/dev/null)
if [ -z "$pids" ]; then
    echo "没有发现 claude 进程"
    exit 0
fi
echo "发现 claude 进程："
echo "$pids" | xargs -I{} ps -p {} -o pid,ppid,cmd --no-headers
kill $pids 2>/dev/null
sleep 1
# 仍有存活的，强杀
remaining=$(pgrep -f '^claude ' 2>/dev/null)
if [ -n "$remaining" ]; then
    echo "以下进程仍在运行，强制杀掉：$remaining"
    kill -9 $remaining 2>/dev/null
fi
echo "完成"
