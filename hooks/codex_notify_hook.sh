#!/usr/bin/env bash
# Codex CLI Stop Hook — 完成通知写入器

set -euo pipefail

QUEUE_FILE="/tmp/claude_perm_queue.jsonl"
LOG_FILE="/tmp/codex_hook_events.log"

INPUT="$(cat)"
PERM_ID="notify_codex_$(echo "${INPUT}$(date +%s%N)" | sha256sum | cut -c1-10)"

ENTRY=$(
    echo "$INPUT" | PERM_ID="$PERM_ID" python3 -c "
import json, os, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

data['id'] = os.environ.get('PERM_ID', 'notify_codex_unknown')
data['type'] = 'notify'
data['agent_source'] = 'codex'
data.setdefault('hook_event_name', 'Stop')
msg = data.get('last_assistant_message')
if isinstance(msg, str):
    msg = msg.strip()
if not msg:
    msg = 'Codex 已完成当前一轮任务，等待您的下一步指令。'
data['message'] = msg[:400]
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null
) || ENTRY="{\"id\":\"${PERM_ID}\",\"type\":\"notify\",\"agent_source\":\"codex\",\"hook_event_name\":\"Stop\",\"message\":\"Codex 已完成当前一轮任务，等待您的下一步指令。\"}"

echo "$ENTRY" >> "$QUEUE_FILE"
rm -f /tmp/codex_always_allow
printf '[%s] stop cleared always_allow\n' "$(date '+%F %T')" >> "$LOG_FILE" 2>/dev/null || true

echo '{"continue":true}'
