#!/usr/bin/env bash
# Agents Island — Windows 实机端到端自测（从 WSL 驱动）
# 前提：桥以 --debug 运行、island.py 已在 Windows 启动
# 流程：注入伪审批 → 等岛自动弹出 → 截屏存证 → API 代答清场
set -e
cd "$(dirname "$0")/.."

SHOT_DIR="${1:-/tmp/island_e2e}"
mkdir -p "$SHOT_DIR"

snap() {  # snap <文件名> <高度>
  (cd /mnt/c && powershell.exe -NoProfile -Command "
Add-Type -AssemblyName System.Drawing
\$b = New-Object System.Drawing.Bitmap(2160, $2)
\$g = [System.Drawing.Graphics]::FromImage(\$b)
\$g.CopyFromScreen(0, 0, 0, 0, \$b.Size)
\$b.Save('$(wslpath -w "$SHOT_DIR")\\$1')
\$g.Dispose(); \$b.Dispose()" 2>/dev/null)
}

echo "[1] 静置态截屏（sliver）"
snap "e2e_1_sliver.png" 60

echo "[2] 注入伪审批"
ID=$(curl -s -X POST localhost:5599/api/test/enqueue \
  -d '{"tool_name":"Bash","tool_input":{"command":"git push origin main --force"}}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "    id=$ID"

sleep 3
echo "[3] 审批弹出态截屏"
snap "e2e_2_approval.png" 320

echo "[4] API 代答 deny 清场"
curl -s -X POST localhost:5599/api/decision -d "{\"id\":\"$ID\",\"decision\":\"deny\"}" > /dev/null
rm -f /tmp/claude_perm_responses/${ID}.json
sleep 2
snap "e2e_3_after.png" 120

echo "[done] 截图在 $SHOT_DIR"
