#!/usr/bin/env bash
# Claude Code PreToolUse Hook — 权限审批拦截器
# 安装后，每次 Claude Code 调用工具前执行本脚本。
# 本脚本将工具调用信息写入队列文件，等待 Agent Monitor 弹窗响应。
#
# 安装方法：将以下内容加入 ~/.claude/settings.json 的 hooks 节：
#   "hooks": {
#     "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/pre_tool_use.sh"}]}]
#   }
#
# Claude Code hooks 协议：
#   - stdin:  JSON {"session_id":"...","tool_name":"...","tool_input":{...}}
#   - stdout: JSON {"decision":"allow"} 或 {"decision":"deny","reason":"..."}
#   - 退出码 0 = 继续；非0 = 阻止

set -e

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
TIMEOUT=35       # 等待响应的最大秒数
DEFAULT="allow"  # 超时后默认决定

# ── 读取 stdin（Claude Code 传入的工具调用信息）───────────────
INPUT=$(cat)

# 生成唯一 ID
PERM_ID="$(echo "$INPUT" | sha256sum | cut -c1-12)_$(date +%s)"

# 注入 ID（env var 传给 python3，避免字符串插值注入）
ENTRY=$(echo "$INPUT" | HOOK_PERM_ID="$PERM_ID" python3 -c "
import sys, json, os
data = json.load(sys.stdin)
data['id'] = os.environ.get('HOOK_PERM_ID', '')
src = os.environ.get('ISLAND_AGENT_SOURCE', '').strip().lower()
if src:
    data['agent_source'] = src   # claude-fork 分支 CLI 来源标记（岛上独立分组）
print(json.dumps(data))
" 2>/dev/null || echo "$INPUT")

# 追加到队列文件（超过 500 行时轮转，防止磁盘无限增长）
LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"

# ── AskUserQuestion：选择题给更长作答窗口（岛上作答特性）──────
TOOL_NAME=$(echo "$INPUT" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null)
if [[ "$TOOL_NAME" == "AskUserQuestion" ]]; then
    TIMEOUT=120   # 超时仍默认 allow → 问题回落终端 TUI，安全兜底
fi

# ── 等待响应 ─────────────────────────────────────────────────
mkdir -p "$RESP_DIR"
RESP_FILE="$RESP_DIR/${PERM_ID}.json"

WAITED=0
while [[ $WAITED -lt $TIMEOUT ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        # 读取决定 + 自定义 reason（岛上作答通道：deny+reason 把用户选择传回模型）。
        # 解析失败重试 3 次（防撞上写入中间态；桥侧已原子写，此为纵深防御）；
        # 仍失败 → 按超时语义 defer。决不兜底 allow：曾会把用户 deny 反转成放行。
        RESP_JSON=""
        for _try in 1 2 3; do
            if RESP_JSON=$(python3 -c "
import json
with open('$RESP_FILE') as f:
    d = json.load(f)
print(json.dumps({
    'decision': d.get('decision', '${DEFAULT}'),
    'reason': d.get('reason', 'User denied via Agents Island'),
}, ensure_ascii=False))
" 2>/dev/null); then
                break
            fi
            RESP_JSON=""
            sleep 0.3
        done
        rm -f "$RESP_FILE"
        if [[ -z "$RESP_JSON" ]]; then
            echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
            exit 0
        fi

        echo "$RESP_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
decision = d.get('decision', 'allow')
out = {'hookSpecificOutput': {'hookEventName': 'PreToolUse'}}
if decision == 'deny':
    out['hookSpecificOutput']['permissionDecision'] = 'deny'
    out['hookSpecificOutput']['permissionDecisionReason'] = d.get('reason') or 'User denied via Agents Island'
else:
    out['hookSpecificOutput']['permissionDecision'] = 'allow'
print(json.dumps(out, ensure_ascii=False))
"
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# 超时 → defer：回落 Claude Code 正常权限流（白名单工具照常自动跑，
# 非白名单工具回终端提问）。决不因无人值守而静默放行。
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
exit 0
