#!/usr/bin/env bash
# Kimi CLI Stop / Notification Hook — 通知写入器（Agents Island）
# 完成一轮任务/通知事件 → 岛上 toast；同时清除 Always Allow 标志。
# 只写队列立即退出，不阻塞 Kimi。

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
ALWAYS_FLAG="${ISLAND_ALWAYS_KIMI:-$STATE_DIR/always_kimi}"

INPUT=$(cat)
PERM_ID="notify_$(echo "${INPUT}$(date +%s%N)" | sha256sum | cut -c1-10)"

ENTRY=$(echo "$INPUT" | PERM_ID="$PERM_ID" python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
data['id']   = os.environ.get('PERM_ID', 'notify_unknown')
data['type'] = 'notify'
data['agent_source'] = 'kimi'
data.setdefault('hook_event_name', 'stop')
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null) || ENTRY="{\"id\":\"${PERM_ID}\",\"type\":\"notify\",\"agent_source\":\"kimi\",\"hook_event_name\":\"stop\"}"

echo "$ENTRY" >> "$QUEUE_FILE"

# 一轮结束，Always Allow 失效，下轮重新询问
if echo "$INPUT" | grep -q '"Stop"'; then
    rm -f "$ALWAYS_FLAG"
fi

exit 0
