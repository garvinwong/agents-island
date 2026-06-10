#!/usr/bin/env bash
# Agents Island — WSL 桥启动脚本（幂等：已在跑则直接退出）
# 用法: bash start_bridge.sh [--debug]
cd "$(dirname "$0")/.." || exit 1   # 自锚定（铁律：禁止依赖调用方 cwd）

PORT=5599
if curl -s -m 2 "http://127.0.0.1:${PORT}/api/health" | grep -q '"ok"'; then
    echo "[island] bridge already up on :${PORT}"
    exit 0
fi

nohup python3 bridge/island_bridge.py "$@" > /tmp/island_bridge.out 2>&1 &
echo "[island] bridge starting (pid $!) log=/tmp/island_bridge.log"

# 等待就绪（最多 15s）
for _ in $(seq 1 30); do
    if curl -s -m 2 "http://127.0.0.1:${PORT}/api/health" | grep -q '"ok"'; then
        echo "[island] bridge ready"
        exit 0
    fi
    sleep 0.5
done
echo "[island] WARN: bridge not ready in 15s, check /tmp/island_bridge.log" >&2
exit 1
