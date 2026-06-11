#!/usr/bin/env bash
# Agents Island — statusLine 包装脚本
# 职责：把 Claude Code statusLine 输入中的官方 rate_limits（5h/7d 用量）缓存给岛，
#       然后原样转发给用户原有的 statusline delegate（HUD 显示不受影响）。
# 安装：scripts/install_statusline.py（自动包装现有 statusLine 命令为 delegate）

CACHE="/tmp/island_rl.json"
DELEGATE_FILE="$(dirname "$0")/statusline_delegate.txt"

INPUT=$(cat)

# 缓存 rate_limits（无 jq 依赖，python3 解析）
printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    rl = d.get('rate_limits')
    if rl:
        with open('$CACHE', 'w') as f:
            json.dump(rl, f)
except Exception:
    pass
" 2>/dev/null

# 转发给原 delegate（保持用户既有 HUD）；无 delegate 则输出极简状态
if [[ -f "$DELEGATE_FILE" ]]; then
    DELEGATE=$(head -1 "$DELEGATE_FILE")
    if [[ -n "$DELEGATE" ]]; then
        printf '%s' "$INPUT" | bash -c "$DELEGATE"
        exit 0
    fi
fi
printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(f\"[{d.get('model',{}).get('display_name','Claude')}]\")
except Exception:
    print('[Claude]')
" 2>/dev/null
