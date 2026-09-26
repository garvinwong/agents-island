#!/usr/bin/env bash
# Agents Island — statusLine 包装脚本
# 职责：把 Claude Code statusLine 输入中的官方 rate_limits（5h/7d 用量）与
#       各会话上下文占用（context_window.used_percentage → ctx/<session_id>.json）
#       缓存给岛，然后原样转发给用户原有的 statusline delegate（HUD 显示不受影响）。
# 安装：scripts/install_statusline.py（自动包装现有 statusLine 命令为 delegate）

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
CACHE="${ISLAND_RL_CACHE:-$STATE_DIR/rl.json}"
CTX_DIR="$STATE_DIR/ctx"
DELEGATE_FILE="${ISLAND_STATUSLINE_DELEGATE_FILE:-$(dirname "$0")/statusline_delegate.txt}"

INPUT=$(cat)

# 缓存 rate_limits + 本会话上下文占用（无 jq 依赖，python3 解析；路径经 env 传入）
printf '%s' "$INPUT" | HOOK_RL_CACHE="$CACHE" HOOK_CTX_DIR="$CTX_DIR" python3 -c "
import json, os, re, sys, time
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
try:
    rl = d.get('rate_limits')
    if rl:
        with open(os.environ['HOOK_RL_CACHE'], 'w') as f:
            json.dump(rl, f)
except Exception:
    pass
try:
    pct = (d.get('context_window') or {}).get('used_percentage')
    sid = d.get('session_id') or os.path.splitext(os.path.basename(d.get('transcript_path') or ''))[0]
    # 首轮未计数时为空不写；会话 id 只认字母数字与连字符（防路径穿越）
    if isinstance(pct, (int, float)) and not isinstance(pct, bool) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', sid or ''):
        cdir = os.environ['HOOK_CTX_DIR']
        os.makedirs(cdir, exist_ok=True)
        tmp = os.path.join(cdir, '.' + sid + '.tmp')
        with open(tmp, 'w') as f:
            json.dump({'pct': int(round(pct)), 'ts': int(time.time())}, f)
        os.replace(tmp, os.path.join(cdir, sid + '.json'))   # 原子替换：桥随时在读
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
