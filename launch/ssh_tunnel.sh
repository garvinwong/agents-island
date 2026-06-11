#!/usr/bin/env bash
# Agents Island — SSH 远程桥隧道守护（WSL 内执行）
# 把远程桥（服务器 127.0.0.1:5599）映射到本地端口，断线自动重连。
#
# 用法: bash ssh_tunnel.sh [本地端口] [ssh目标]
#   默认: 本地 5598 ← user@your-server:2222 的 5599
# 幂等: 已有同端口隧道存活则直接退出
# 配套: 本地桥 island_settings.json 加
#   "remotes": [{"name":"gcp","url":"http://127.0.0.1:5598",
#                "ssh":"ssh -t -p 2222 user@your-server"}]

LOCAL_PORT="${1:-5598}"
SSH_TARGET="${2:-user@your-server}"
SSH_PORT="${3:-22}"
REMOTE_PORT="${4:-5599}"
LOG="/tmp/island_tunnel_${LOCAL_PORT}.log"

# 幂等：端口已通（无论谁开的）就不再起
if curl -s -m 2 "http://127.0.0.1:${LOCAL_PORT}/api/health" >/dev/null 2>&1; then
    echo "[tunnel] 本地 ${LOCAL_PORT} 已通，跳过"
    exit 0
fi
if pgrep -f "ssh .*-L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" >/dev/null; then
    echo "[tunnel] 隧道进程已在（等待建立），跳过"
    exit 0
fi

echo "[tunnel] ${LOCAL_PORT} ← ${SSH_TARGET}:${REMOTE_PORT} 守护启动，日志 $LOG"
nohup bash -c "
while true; do
    ssh -p ${SSH_PORT} -N \
        -L ${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT} \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
        -o ExitOnForwardFailure=yes -o ConnectTimeout=10 \
        -o BatchMode=yes \
        ${SSH_TARGET} >> '$LOG' 2>&1
    echo \"\$(date '+%F %T') tunnel exited, retry in 8s\" >> '$LOG'
    sleep 8
done" >/dev/null 2>&1 &
disown
echo "[tunnel] ok (pid $!)"
