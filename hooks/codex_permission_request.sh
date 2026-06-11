#!/usr/bin/env bash
# Codex CLI PermissionRequest Hook — 审批拦截器（Agents Island）
# 捕获 Codex 的 exec/patch 审批请求（终端"Would you like to make the following
# edits?"那类提问），上岛裁决。PreToolUse 只覆盖 Bash；编辑审批走本事件。
#
# Codex hooks 协议（codex-cli >= 0.139，二进制内嵌 JSON Schema 取证）：
#   - stdin : JSON（含 hook_event_name=PermissionRequest、工具/补丁详情）
#   - stdout: {"hookSpecificOutput":{"hookEventName":"PermissionRequest",
#              "decision":{"behavior":"allow"|"deny","message":"..."}}}
#   - 无输出 exit 0 = 不裁决 → 回落 Codex 终端审批（安全兜底）
#   - ⚠️ interrupt/updatedInput/updatedPermissions 为保留字段，带上会 fail-closed

set -euo pipefail

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
ALWAYS_FLAG="${ISLAND_ALWAYS_CODEX:-$STATE_DIR/always_codex}"
TIMEOUT=35

INPUT="$(cat)"

# ── Always Allow 标志（TTL 4h，Stop hook 清除）────────────────────────
if [[ -f "$ALWAYS_FLAG" ]]; then
    FLAG_AGE=$(( $(date +%s) - $(stat -c %Y "$ALWAYS_FLAG" 2>/dev/null || echo 0) ))
    if [[ $FLAG_AGE -lt 14400 ]]; then
        echo '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow","message":"Agents Island always-allow"}}}'
        exit 0
    fi
    rm -f "$ALWAYS_FLAG"
fi

# ── 入队（提炼工具名/摘要供岛上展示）─────────────────────────────────
PERM_ID="codexpr_$(echo "$INPUT" | sha256sum | cut -c1-12)_$(date +%s)"
ENTRY=$(echo "$INPUT" | HOOK_PERM_ID="$PERM_ID" python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
data['id'] = os.environ.get('HOOK_PERM_ID', '')
data['agent_source'] = 'codex'
# 展示友好化：PermissionRequest 载荷里可能没有 tool_name，按内容推断
if not data.get('tool_name'):
    blob = json.dumps(data, ensure_ascii=False)
    if 'patch' in blob or 'file_change' in blob or 'fileChange' in blob or 'changes' in blob:
        data['tool_name'] = 'ApplyPatch'
    elif 'command' in blob or 'exec' in blob:
        data['tool_name'] = 'Exec'
    else:
        data['tool_name'] = 'PermissionRequest'
print(json.dumps(data, ensure_ascii=False))
" 2>/dev/null || echo "$INPUT")

LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"

# ── 等待岛侧响应 ─────────────────────────────────────────────────────
mkdir -p "$RESP_DIR"
RESP_FILE="$RESP_DIR/${PERM_ID}.json"

WAITED=0
while [[ $WAITED -lt $TIMEOUT ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        python3 - "$RESP_FILE" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    d = {}
decision = d.get('decision')
if decision in ('allow', 'deny'):
    out = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest',
           'decision': {'behavior': decision}}}
    reason = d.get('reason')
    if reason:
        out['hookSpecificOutput']['decision']['message'] = reason
    print(json.dumps(out, ensure_ascii=False))
PYEOF
        rm -f "$RESP_FILE"
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# 超时：不裁决 → 回落 Codex 终端审批（决不静默放行）
exit 0
