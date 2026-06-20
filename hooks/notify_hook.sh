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

# Working 结束时清除 Always Allow 状态，下次对话重新询问
SRC="${ISLAND_AGENT_SOURCE:-claude}"
if [[ "$SRC" == "claude" ]]; then
    rm -f "${ISLAND_ALWAYS_CLAUDE:-$STATE_DIR/always_claude}"
else
    rm -f "$STATE_DIR/always_${SRC}"
fi

exit 0
