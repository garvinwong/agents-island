#!/usr/bin/env bash
# Kimi CLI PreToolUse Hook — 权限审批拦截器（Agents Island）
# 安装：python3 scripts/install_kimi_hooks.py（写入 ~/.kimi/config.toml hooks 数组）
#
# Kimi CLI hooks 协议（kimi-cli >= 1.45，源码 kimi_cli/hooks/runner.py）：
#   - stdin : JSON {"hook_event_name":"PreToolUse","session_id","cwd","tool_name","tool_input","tool_call_id"}
#   - stdout: JSON {"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"..."}} = 阻止
#   - 其余（exit 0 无输出 / 非 deny）= 放行；exit 2 = 阻止（stderr 为理由）
#   - ⚠️ 与 Claude Code 差异：无 ask/defer；Kimi 侧超时/报错一律 fail-open 放行
#
# 超时策略（本脚本 35s 等不到岛响应时）：
#   - ~/.kimi/config.toml default_yolo=true  → 岛是唯一闸门 → 超时 deny（决不静默放行）
#   - default_yolo=false → Kimi 终端审批仍会把关高危工具 → 超时放行回落终端

set -e

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
ALWAYS_FLAG="${ISLAND_ALWAYS_KIMI:-$STATE_DIR/always_kimi}"
KIMI_CONFIG="$HOME/.kimi/config.toml"
TIMEOUT=35

INPUT=$(cat)

# ── Always Allow 标志（岛上按 S/Always 写入，Stop hook 清除，TTL 4h）──
if [[ -f "$ALWAYS_FLAG" ]]; then
    FLAG_AGE=$(( $(date +%s) - $(stat -c %Y "$ALWAYS_FLAG" 2>/dev/null || echo 0) ))
    if [[ $FLAG_AGE -lt 14400 ]]; then
        exit 0
    fi
    rm -f "$ALWAYS_FLAG"
fi

# ── AskUserQuestion：选择题给更长作答窗口（岛上作答特性，入参与 Claude 同构）──
TOOL_NAME=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)
if [[ "$TOOL_NAME" == "AskUserQuestion" ]]; then
    TIMEOUT=120   # 超时放行 → 问题回落 Kimi 终端 TUI，安全兜底
fi

# ── 入队（带 agent_source=kimi 标识，岛上显示紫色分组）────────────────
PERM_ID="$(echo "$INPUT" | sha256sum | cut -c1-12)_$(date +%s)"
ENTRY=$(echo "$INPUT" | HOOK_PERM_ID="$PERM_ID" python3 -c "
import sys, json, os
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
data['id'] = os.environ.get('HOOK_PERM_ID', '')
data['agent_source'] = 'kimi'
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
if d.get('decision') == 'deny':
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': d.get('reason') or 'User denied via Agents Island',
    }}, ensure_ascii=False))
PYEOF
        rm -f "$RESP_FILE"
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# ── 超时：按 yolo 模式分流（见文件头注释）────────────────────────────
if grep -qE '^\s*default_yolo\s*=\s*true' "$KIMI_CONFIG" 2>/dev/null; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Agents Island 审批超时；yolo 模式下无终端兜底，安全拒绝。请在岛上或终端重试。"}}'
fi
exit 0
