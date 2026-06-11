#!/usr/bin/env bash
# Agents Island — 远程桥部署（部署到 SSH 服务器，供岛远程监控）
#
# 服务器侧形态：
#   ~/services/island-bridge/   bridge/ + hooks/ + scripts/（自包含）
#   systemd 用户服务 island-bridge（127.0.0.1:5599，决不绑公网 ←信息安全铁律）
#   hooks 装进服务器的 ~/.claude/settings.json（在服务器跑 claude 即上岛）
#
# 用法: bash deploy_remote_bridge.sh [user@host] [ssh端口]
#   默认 user@your-server 2222
set -euo pipefail
cd "$(dirname "$0")/.."   # ⚠️ 自锚定到 app 根（部署脚本铁律：rsync 相对路径）

TARGET="${1:-user@your-server}"
PORT="${2:-22}"
DEST="~/services/island-bridge"

echo "── rsync 桥与 hooks → ${TARGET}:${DEST}"
ssh -p "$PORT" "$TARGET" "mkdir -p ${DEST}"
rsync -az -e "ssh -p $PORT" \
    --include='bridge/***' --include='hooks/***' \
    --include='scripts/install_hooks.py' --include='scripts/' \
    --exclude='*' \
    ./ "${TARGET}:${DEST}/"

echo "── 安装 systemd 用户服务并启动"
ssh -p "$PORT" "$TARGET" 'bash -s' <<'REMOTE'
set -e
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/island-bridge.service <<'UNIT'
[Unit]
Description=Agents Island remote bridge (loopback only)
After=network.target

[Service]
ExecStart=/usr/bin/python3 %h/services/island-bridge/bridge/island_bridge.py --port 5599
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now island-bridge
loginctl enable-linger "$USER" 2>/dev/null || true
sleep 2
curl -s -m 3 http://127.0.0.1:5599/api/health && echo " <- bridge 健康" || { echo "❌ 桥未起来"; exit 1; }
# hooks 装进服务器 Claude Code（幂等）
python3 ~/services/island-bridge/scripts/install_hooks.py || true
REMOTE

echo "✅ 远程桥部署完成。本地侧接线："
echo "   1) bash launch/ssh_tunnel.sh        # 建隧道 5598←5599"
echo "   2) island_settings.json 加 remotes（见 ssh_tunnel.sh 头注）"
