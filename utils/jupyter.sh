#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== 启动 Jupyter Lab ==="
nohup jupyter lab \
  --ip=0.0.0.0 \
  --port=8888 \
  --no-browser \
  --ServerApp.token='' \
  --ServerApp.password='' \
  --allow-root \
  > jupyter.log 2>&1 &

echo "Jupyter Lab 已启动, PID: $!"
echo "日志文件: $(pwd)/jupyter.log"
echo "访问地址: http://0.0.0.0:8888"
