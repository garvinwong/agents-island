#!/usr/bin/env bash
# Codex CLI PreToolUse Hook — 审批拦截器
# 当前 Codex 官方仅对 Bash 提供 PreToolUse。

set -euo pipefail

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
ALWAYS_ALLOW_FLAG="${ISLAND_ALWAYS_CODEX:-$STATE_DIR/always_codex}"
LOG_FILE="/tmp/codex_hook_events.log"
TIMEOUT=35

INPUT="$(cat)"
printf '[%s] PreToolUse raw=%s\n' "$(date '+%F %T')" "${INPUT:0:400}" >> "$LOG_FILE" 2>/dev/null || true

CURRENT_SESSION_ID=$(
    printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
print(data.get('session_id', ''))
" 2>/dev/null
)

if [[ -f "$ALWAYS_ALLOW_FLAG" ]]; then
    FLAG_STATE=$(
        CURRENT_SESSION_ID="$CURRENT_SESSION_ID" ALWAYS_ALLOW_FLAG="$ALWAYS_ALLOW_FLAG" python3 -c "
import json, os, time
from pathlib import Path

flag = Path(os.environ['ALWAYS_ALLOW_FLAG'])
current_sid = os.environ.get('CURRENT_SESSION_ID', '')
ttl = 4 * 60 * 60

try:
    data = json.loads(flag.read_text(encoding='utf-8'))
except Exception:
    print('legacy_or_invalid')
    raise SystemExit

flag_sid = str(data.get('session_id') or '')
created_at = int(data.get('created_at') or 0)
age = max(0, int(time.time()) - created_at) if created_at else 0

if not flag_sid:
    print('missing_session')
elif current_sid and flag_sid != current_sid:
    print('session_mismatch')
elif created_at and age > ttl:
    print('expired')
else:
    print('active')
" 2>/dev/null || echo "legacy_or_invalid"
    )

    if [[ "$FLAG_STATE" == "active" ]]; then
        printf '[%s] skip id=pending reason=always_allow_active session=%s\n' "$(date '+%F %T')" "$CURRENT_SESSION_ID" >> "$LOG_FILE" 2>/dev/null || true
        exit 0
    fi

    rm -f "$ALWAYS_ALLOW_FLAG"
    printf '[%s] cleared stale always_allow reason=%s session=%s\n' "$(date '+%F %T')" "$FLAG_STATE" "$CURRENT_SESSION_ID" >> "$LOG_FILE" 2>/dev/null || true
fi

PERM_ID="codex_$(echo "$INPUT" | sha256sum | cut -c1-12)_$(date +%s)"

ENTRY=$(
    echo "$INPUT" | HOOK_PERM_ID="$PERM_ID" python3 -c "
import json, os, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = {}

data['id'] = os.environ.get('HOOK_PERM_ID', '')
data['agent_source'] = 'codex'
data.setdefault('hook_event_name', 'PreToolUse')
data.setdefault('tool_name', 'Bash')
tool_input = data.get('tool_input')
if not isinstance(tool_input, dict):
    tool_input = {}
if 'command' not in tool_input and isinstance(data.get('command'), str):
    tool_input['command'] = data['command']
data['tool_input'] = tool_input
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null
) || ENTRY="{\"id\":\"${PERM_ID}\",\"agent_source\":\"codex\",\"hook_event_name\":\"PreToolUse\",\"tool_name\":\"Bash\"}"

LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"
printf '[%s] queued id=%s\n' "$(date '+%F %T')" "$PERM_ID" >> "$LOG_FILE" 2>/dev/null || true

mkdir -p "$RESP_DIR"
RESP_FILE="$RESP_DIR/${PERM_ID}.json"

WAITED=0
while [[ $WAITED -lt $TIMEOUT ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        DECISION=$(
            python3 -c "
import json
with open('$RESP_FILE', 'r', encoding='utf-8') as f:
    data = json.load(f)
print(data.get('decision', 'allow'))
" 2>/dev/null || echo "allow"
        )
        rm -f "$RESP_FILE"
        printf '[%s] response id=%s decision=%s\n' "$(date '+%F %T')" "$PERM_ID" "$DECISION" >> "$LOG_FILE" 2>/dev/null || true

        if [[ "$DECISION" == "deny" ]]; then
            cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"User denied via Agent Monitor"}}
EOF
        fi
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

printf '[%s] timeout id=%s auto-allow\n' "$(date '+%F %T')" "$PERM_ID" >> "$LOG_FILE" 2>/dev/null || true
exit 0
