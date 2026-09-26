#!/usr/bin/env bash
# Claude Code Stop / Notification Hook — 通知队列写入器
# 当 Claude 完成回复或等待用户输入时，将事件写入队列，由 Agent Monitor 弹窗通知。
# 本脚本仅写入，立即退出，不阻塞 Claude Code 执行。
#
# 配置方式（~/.claude/settings.json）：
#   "Stop":         [{"matcher":"","hooks":[{"type":"command","command":"/path/to/notify_hook.sh"}]}]
#   "Notification": [{"matcher":"","hooks":[{"type":"command","command":"/path/to/notify_hook.sh"}]}]

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"

INPUT=$(cat)

# 生成唯一 ID（前缀 notify_ 供 monitor.py 识别类型）
PERM_ID="notify_$(echo "${INPUT}$(date +%s%N)" | sha256sum | cut -c1-10)"

# 注入 id 和 type:notify，同时保留原始字段
# 注意：PERM_ID 必须作为环境变量前缀传入（写成 python3 -c "..." PERM_ID=xxx 后缀
# 只是 argv，os.environ 取不到 → 退化成 notify_unknown，所有 notify 撞同一 id 被
# bridge 去重丢弃，弹窗/声效全失效）。ISLAND_AGENT_SOURCE 同理显式前缀导出。
ENTRY=$(echo "$INPUT" | PERM_ID="$PERM_ID" ISLAND_AGENT_SOURCE="${ISLAND_AGENT_SOURCE:-}" python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
data['id']   = os.environ.get('PERM_ID', 'notify_unknown')
data['type'] = 'notify'
src = os.environ.get('ISLAND_AGENT_SOURCE', '').strip().lower()
if src:
    data['agent_source'] = src
# hook_event_name 供弹窗显示事件来源
if 'hook_event_name' not in data:
    data['hook_event_name'] = 'stop'
print(json.dumps(data))
" 2>/dev/null) || ENTRY="{\"id\":\"${PERM_ID}\",\"type\":\"notify\",\"hook_event_name\":\"stop\"}"

echo "$ENTRY" >> "$QUEUE_FILE"

# 一轮结束（Stop）时清除 Always Allow 状态，下次对话重新询问。
# 2026-09-26：只清本会话点的那枚——旧版任何会话答完一轮（甚至只是 Notification
# 提醒）都会清掉，与"Always 只对点它的会话生效"不符；旧格式标志（无 session_id）
# 维持原语义，任一会话一轮结束即清。
SRC="${ISLAND_AGENT_SOURCE:-claude}"
if [[ "$SRC" == "claude" ]]; then
    FLAG="${ISLAND_ALWAYS_CLAUDE:-$STATE_DIR/always_claude}"
else
    FLAG="$STATE_DIR/always_${SRC}"
fi
if [[ -f "$FLAG" ]]; then
    printf '%s' "$INPUT" | HOOK_FLAG="$FLAG" python3 -c '
import json, os, sys
try:
    ev = json.load(sys.stdin)
except Exception:
    ev = {}
if str(ev.get("hook_event_name") or "stop").lower() != "stop":
    sys.exit(0)                                  # Notification 等：不是一轮结束
flag = os.environ["HOOK_FLAG"]
try:
    with open(flag, encoding="utf-8") as f:
        d = json.load(f)
    fsid = str(d.get("session_id") or "") if isinstance(d, dict) else ""
except Exception:
    fsid = ""                                    # 读坏按旧语义清掉
if not fsid or fsid == str(ev.get("session_id") or ""):
    try:
        os.remove(flag)
    except OSError:
        pass
' 2>/dev/null || true
fi

exit 0
