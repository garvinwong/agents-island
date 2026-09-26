#!/usr/bin/env bash
# Claude Code PreToolUse Hook — 权限审批拦截器
# 安装后，每次 Claude Code 调用工具前执行本脚本。
# 本脚本将工具调用信息写入队列文件，等待 Agents Island 岛上审批。
#
# 安装方法：将以下内容加入 ~/.claude/settings.json 的 hooks 节：
#   "hooks": {
#     "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/pre_tool_use.sh"}]}]
#   }
#
# Claude Code hooks 协议：
#   - stdin:  JSON {"session_id":"...","tool_name":"...","tool_input":{...}}
#   - stdout: JSON {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#                   "permissionDecision":"allow|deny|defer","permissionDecisionReason":"..."}}
#   - 退出码 0 = 继续；非0 = 阻止
#
# 快速通道（2026-09-26）：本会话已开 ⚡YOLO 或点过 Always → 钩子自己判定放行，
# 不排队、不等桥（旧版一律排队，钩子每 1 秒才看一次答复，最近 12 个会话 4003 次
# 放行耗时中位 1.13s，累计约 95 分钟纯等待）。选择题/计划永不走快速通道。

set -e

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR"
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
# 默认 35s 不变；TG 长程任务经 env 注入 ISLAND_HOOK_TIMEOUT=600 延长审批窗口
# （移动端 35s 不够）。仅影响注入该 env 的子进程，本机终端会话行为零变化。
TIMEOUT="${ISLAND_HOOK_TIMEOUT:-35}"   # 等待响应的最大秒数
DEFAULT="allow"  # 答复文件缺 decision 字段时的取值

# ── 读取 stdin（Claude Code 传入的工具调用信息）───────────────
INPUT=$(cat)

# 生成唯一 ID
PERM_ID="$(echo "$INPUT" | sha256sum | cut -c1-12)_$(date +%s)"

# 一次 python 完成：注入 id/来源、取工具名、判快速通道（旧版为此起 2 次 python）。
# 输出第 1 行 = "<yolo|always|空>\t<工具名>"，第 2 行 = 入队 JSON。
# 值一律经 env 传入，避免字符串插值注入。判定规则与桥 add_entry 一致。
PARSED=$(printf '%s' "$INPUT" | HOOK_PERM_ID="$PERM_ID" HOOK_STATE_DIR="$STATE_DIR" python3 -c '
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
data["id"] = os.environ.get("HOOK_PERM_ID", "")
src = os.environ.get("ISLAND_AGENT_SOURCE", "").strip().lower()
if src:
    data["agent_source"] = src   # claude-fork 分支 CLI 来源标记（岛上独立分组）
agent = src or "claude"
tool = str(data.get("tool_name") or "")
sid = str(data.get("session_id") or "")
st = os.environ["HOOK_STATE_DIR"]
fast = ""
if sid and tool not in ("AskUserQuestion", "ExitPlanMode"):
    try:
        with open(os.path.join(st, "yolo_sessions.json"), encoding="utf-8") as f:
            ids = json.load(f)
        if isinstance(ids, list) and sid in ids:
            fast = "yolo"
    except Exception:
        pass
    if not fast:
        flag = (os.environ.get("ISLAND_ALWAYS_CLAUDE") if agent == "claude" else "") \
            or os.path.join(st, "always_" + agent)
        try:
            with open(flag, encoding="utf-8") as f:
                d = json.load(f)
            fsid = str(d.get("session_id") or "") if isinstance(d, dict) else ""
            if not fsid or fsid == sid:      # 只认点 Always 的会话；旧格式无 sid 维持全局
                fast = "always"
        except Exception:
            pass
print(fast + "\t" + tool)
print(json.dumps(data))
' 2>/dev/null) || PARSED=""

if [[ "$PARSED" == *$'\n'* ]]; then
    HEAD="${PARSED%%$'\n'*}"
    ENTRY="${PARSED#*$'\n'}"
    FAST="${HEAD%%$'\t'*}"
    TOOL_NAME="${HEAD#*$'\t'}"
else
    HEAD=""; FAST=""; TOOL_NAME=""; ENTRY="$INPUT"   # 解析失败：原样入队交给桥
fi

# ── 快速通道：直接放行，只记一行流水（不经桥）────────────────
if [[ "$FAST" == "yolo" || "$FAST" == "always" ]]; then
    FP_LOG="$STATE_DIR/fastpath.log"
    if [[ -f "$FP_LOG" ]] && [[ $(stat -c %s "$FP_LOG" 2>/dev/null || echo 0) -gt 2000000 ]]; then
        tail -n 5000 "$FP_LOG" > "$FP_LOG.tmp" 2>/dev/null && mv "$FP_LOG.tmp" "$FP_LOG" || true
    fi
    printf '%s %s %s %s\n' "$(date '+%F %T')" "$FAST" "$TOOL_NAME" "$PERM_ID" >> "$FP_LOG" 2>/dev/null || true
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
    exit 0
fi

# 追加到队列文件（超过 500 行时轮转，防止磁盘无限增长）
LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"

# ── AskUserQuestion：选择题给更长作答窗口（岛上作答特性）──────
if [[ "$TOOL_NAME" == "AskUserQuestion" ]]; then
    TIMEOUT="${ISLAND_HOOK_TIMEOUT_ASK:-120}"   # 超时仍默认 allow → 问题回落终端 TUI，安全兜底
fi

# ── 等待响应 ─────────────────────────────────────────────────
# 前 5 秒每 0.1 秒看一次（点完允许到命令开跑几乎无感；旧版 1 秒一看，平均白等 0.5s），
# 之后每 0.25 秒一次，长等待时少起进程。
mkdir -p "$RESP_DIR"
RESP_FILE="$RESP_DIR/${PERM_ID}.json"

WAITED_MS=0
LIMIT_MS=$((TIMEOUT * 1000))
while [[ $WAITED_MS -lt $LIMIT_MS ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        # 读取决定 + 自定义 reason（岛上作答通道：deny+reason 把用户选择传回模型），
        # 直接生成钩子输出。解析失败重试 3 次（防撞上写入中间态；桥侧已原子写，
        # 此为纵深防御）；仍失败 → 按超时语义 defer。决不兜底 allow：曾会把用户 deny 反转成放行。
        OUT=""
        for _try in 1 2 3; do
            if OUT=$(HOOK_RESP_FILE="$RESP_FILE" HOOK_DEFAULT="$DEFAULT" python3 -c '
import json, os
with open(os.environ["HOOK_RESP_FILE"], encoding="utf-8") as f:
    d = json.load(f)
out = {"hookSpecificOutput": {"hookEventName": "PreToolUse"}}
if d.get("decision", os.environ["HOOK_DEFAULT"]) == "deny":
    out["hookSpecificOutput"]["permissionDecision"] = "deny"
    out["hookSpecificOutput"]["permissionDecisionReason"] = d.get("reason") or "User denied via Agents Island"
else:
    out["hookSpecificOutput"]["permissionDecision"] = "allow"
print(json.dumps(out, ensure_ascii=False))
' 2>/dev/null); then
                break
            fi
            OUT=""
            sleep 0.3
        done
        rm -f "$RESP_FILE"
        if [[ -z "$OUT" ]]; then
            echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
            exit 0
        fi
        echo "$OUT"
        exit 0
    fi
    if [[ $WAITED_MS -lt 5000 ]]; then
        sleep 0.1; WAITED_MS=$((WAITED_MS + 100))
    else
        sleep 0.25; WAITED_MS=$((WAITED_MS + 250))
    fi
done

# 超时 → defer：回落 Claude Code 正常权限流（白名单工具照常自动跑，
# 非白名单工具回终端提问）。决不因无人值守而静默放行。
echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"defer"}}'
exit 0
