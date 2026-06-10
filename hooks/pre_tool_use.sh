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

QUEUE_FILE="/tmp/claude_perm_queue.jsonl"
RESP_DIR="/tmp/claude_perm_responses"
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
print(json.dumps(data))
" 2>/dev/null || echo "$INPUT")

# 追加到队列文件（超过 500 行时轮转，防止磁盘无限增长）
LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"

# ── 等待响应 ─────────────────────────────────────────────────
mkdir -p "$RESP_DIR"
RESP_FILE="$RESP_DIR/${PERM_ID}.json"

WAITED=0
while [[ $WAITED -lt $TIMEOUT ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        # 读取决定
        DECISION=$(python3 -c "
import json, sys
with open('$RESP_FILE') as f:
    d = json.load(f)
print(d.get('decision', '${DEFAULT}'))
" 2>/dev/null || echo "$DEFAULT")
        rm -f "$RESP_FILE"

        if [[ "$DECISION" == "deny" ]]; then
            echo '{"decision":"deny","reason":"User denied via Agent Monitor"}'
        else
            echo '{"decision":"allow"}'
        fi
        exit 0
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# 超时 → 默认决定
if [[ "$DEFAULT" == "deny" ]]; then
    echo '{"decision":"deny","reason":"Timeout - no response from Agent Monitor"}'
else
    echo '{"decision":"allow"}'
fi
exit 0
