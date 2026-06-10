#!/usr/bin/env bash
# Claude Code Stop / Notification Hook — 通知队列写入器
# 当 Claude 完成回复或等待用户输入时，将事件写入队列，由 Agent Monitor 弹窗通知。
# 本脚本仅写入，立即退出，不阻塞 Claude Code 执行。
#
# 配置方式（~/.claude/settings.json）：
#   "Stop":         [{"matcher":"","hooks":[{"type":"command","command":"/path/to/notify_hook.sh"}]}]
#   "Notification": [{"matcher":"","hooks":[{"type":"command","command":"/path/to/notify_hook.sh"}]}]

QUEUE_FILE="/tmp/claude_perm_queue.jsonl"

INPUT=$(cat)

# 生成唯一 ID（前缀 notify_ 供 monitor.py 识别类型）
PERM_ID="notify_$(echo "${INPUT}$(date +%s%N)" | sha256sum | cut -c1-10)"

# 注入 id 和 type:notify，同时保留原始字段
ENTRY=$(echo "$INPUT" | python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
data['id']   = os.environ.get('PERM_ID', 'notify_unknown')
data['type'] = 'notify'
# hook_event_name 供弹窗显示事件来源
if 'hook_event_name' not in data:
    data['hook_event_name'] = 'stop'
print(json.dumps(data))
" PERM_ID="$PERM_ID" 2>/dev/null) || ENTRY="{\"id\":\"${PERM_ID}\",\"type\":\"notify\",\"hook_event_name\":\"stop\"}"

echo "$ENTRY" >> "$QUEUE_FILE"

# Working 结束时清除 Always Allow 状态，下次对话重新询问
rm -f /tmp/claude_always_allow

exit 0
