#!/usr/bin/env bash
# Claude Code PermissionRequest Hook — 终端权限框同步上岛（2026-09-26）
#
# 何时触发：Claude Code 要弹终端权限框之前，含自动模式下内置安全检查那类“必须由人定”的框
#   （PreToolUse 返回允许也压不住它们）。
# 实测（Claude Code 2.1.283 交互式）：终端框与本钩子并行出现，谁先答算谁的——
#   · 岛上点允许/拒绝 → 本钩子输出裁决，终端框随即收掉；
#   · 终端先答 → Claude Code 不会结束本钩子（真机端到端更正：试做里那次 SIGTERM 实为
#     关会话所致）。弹框挂着时会话记录无新增，作答后第一条新增就是该命令的 tool_result，
#     本钩子据此判定“终端已答”→ 写撤卡标记退出，岛上卡片消失；
#   · 无输出 exit 0 = 不裁决 → 终端框照常。任何异常都走这条，决不兜底放行。
# 协议 stdout：{"hookSpecificOutput":{"hookEventName":"PermissionRequest",
#               "decision":{"behavior":"allow"} | {"behavior":"deny","message":"..."}}}
# 不读 YOLO 名单与 Always 标志——这类框是 Claude Code 判定必须由人定的。

STATE_DIR="${ISLAND_STATE_DIR:-$HOME/.agents-island}"
mkdir -p "$STATE_DIR" 2>/dev/null || exit 0
QUEUE_FILE="${ISLAND_QUEUE_FILE:-$STATE_DIR/queue.jsonl}"
RESP_DIR="${ISLAND_RESP_DIR:-$STATE_DIR/responses}"
TIMEOUT="${ISLAND_PR_TIMEOUT:-110}"   # 秒；低于终端框约 2 分钟的自动拒绝

INPUT=$(cat)
# id = pr_<内容摘要>-<进程号>_<秒>：同一秒内一模一样的请求也不撞 id（代码审查#2）；
# 末段保持入队秒数，桥按它计算重启回放窗口
PERM_ID="pr_$(printf '%s' "$INPUT" | sha256sum | cut -c1-12)-$$_$(date +%s)"

# 注入 id / 来源标记（值经 env 传入，避免字符串插值注入）；解析失败直接不裁决。
# 输出第 1 行 = 入队 JSON，第 2 行 = 会话记录路径（用于判定终端是否已答）
PARSED=$(printf '%s' "$INPUT" | HOOK_PERM_ID="$PERM_ID" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
# 选择题/计划由 PreToolUse 走岛上作答，本钩子不插手（代码审查#1：否则多一张只有允许/
# 拒绝的卡，点允许就绕过了岛上作答）
if data.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
    sys.exit(3)
data["id"] = os.environ["HOOK_PERM_ID"]
data.setdefault("hook_event_name", "PermissionRequest")
data["island_perm"] = 1     # 桥据此判 perm、永不自动放行，不依赖来源名（审查#3）
src = os.environ.get("ISLAND_AGENT_SOURCE", "").strip().lower()
if src:
    data["agent_source"] = src
print(json.dumps(data))
print(str(data.get("transcript_path") or ""))
' 2>/dev/null) || exit 0
ENTRY="${PARSED%%$'\n'*}"
TRANSCRIPT="${PARSED#*$'\n'}"
[[ "$PARSED" == *$'\n'* ]] || TRANSCRIPT=""
[[ -n "$ENTRY" ]] || exit 0
# 判定“终端已答”须精确到本次调用：会话记录有写缓冲，本次调用的助手行可能晚于本钩子
# 才落盘（真机实录：按“有新助手行”判会误撤卡）；同一命令也可能以前跑过。做法：开始时记下
# 已完成的调用 id；等待期间找入参相同、开始时尚未完成的 tool_use，它的 tool_result 出现才算。
TRACK=""
if [[ -f "$TRANSCRIPT" ]]; then
    TRACK=$(mktemp "${TMPDIR:-/tmp}/island_pr.XXXXXX" 2>/dev/null) || TRACK=""
fi
tr_probe() {   # $1 = init | check；check 时退出码 0 = 终端已答
    HOOK_TR="$TRANSCRIPT" HOOK_INPUT="$INPUT" HOOK_TRACK="$TRACK" HOOK_MODE="$1" python3 -c '
import json, os, sys
want = (json.loads(os.environ["HOOK_INPUT"]) or {}).get("tool_input")
try:
    with open(os.environ["HOOK_TR"], "rb") as f:
        f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 1048576))
        lines = f.read().decode("utf-8", "replace").splitlines()
except OSError:
    sys.exit(1)
uses, done = [], set()
for line in lines:
    if "tool_use" not in line and "tool_result" not in line:
        continue
    try:
        o = json.loads(line)
    except Exception:
        continue
    content = (o.get("message") or {}).get("content")
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("input") == want:
            uses.append(b.get("id"))
        elif b.get("type") == "tool_result":
            done.add(b.get("tool_use_id"))
if os.environ["HOOK_MODE"] == "init":
    with open(os.environ["HOOK_TRACK"], "w") as f:
        json.dump(sorted(x for x in done if x), f)
    sys.exit(0)
with open(os.environ["HOOK_TRACK"]) as f:
    done0 = set(json.load(f))
sys.exit(0 if any(u and u not in done0 and u in done for u in uses) else 1)
' 2>/dev/null
}
[[ -n "$TRACK" ]] && { tr_probe init || TRACK=""; }
terminal_answered() { [[ -n "$TRACK" ]] && tr_probe check; }

cancel() {   # 撤卡标记：桥见到即把这张卡从岛上撤掉
    printf '{"type": "cancel", "id": "%s"}\n' "$PERM_ID" >> "$QUEUE_FILE" 2>/dev/null
    rm -f "$RESP_DIR/${PERM_ID}.json"
}
trap 'cancel; exit 0' TERM INT HUP
trap '[[ -n "$TRACK" ]] && rm -f "$TRACK"' EXIT

# 追加到队列文件（超过 500 行时轮转，与其他钩子一致）
LINE_COUNT=$(wc -l < "$QUEUE_FILE" 2>/dev/null || echo 0)
if [[ "$LINE_COUNT" -gt 500 ]]; then
    tail -n 200 "$QUEUE_FILE" > "${QUEUE_FILE}.tmp" && mv "${QUEUE_FILE}.tmp" "$QUEUE_FILE"
fi
echo "$ENTRY" >> "$QUEUE_FILE"

# 等岛上答复：前 5 秒每 0.1 秒看一次，之后每 0.25 秒
mkdir -p "$RESP_DIR" 2>/dev/null
RESP_FILE="$RESP_DIR/${PERM_ID}.json"
WAITED_MS=0
LIMIT_MS=$((TIMEOUT * 1000))
while [[ $WAITED_MS -lt $LIMIT_MS ]]; do
    if [[ -f "$RESP_FILE" ]]; then
        # 解析失败重试 3 次（桥侧已原子写，此为纵深防御）；仍失败 → 不裁决
        OUT=""
        for _try in 1 2 3; do
            if OUT=$(HOOK_RESP_FILE="$RESP_FILE" python3 -c '
import json, os
with open(os.environ["HOOK_RESP_FILE"], encoding="utf-8") as f:
    d = json.load(f)
dec = d.get("decision")
if dec == "deny":
    out = {"behavior": "deny", "message": d.get("reason") or "用户在灵动岛上拒绝了这次操作"}
elif dec in ("allow", "always"):
    out = {"behavior": "allow"}
else:
    raise SystemExit(1)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                         "decision": out}}, ensure_ascii=False))
' 2>/dev/null); then
                break
            fi
            OUT=""
            sleep 0.3
        done
        rm -f "$RESP_FILE"
        [[ -n "$OUT" ]] && echo "$OUT"
        cancel    # 完成标记：桥重启回放时不让答过的卡复活（代码审查#5）
        exit 0
    fi
    if (( WAITED_MS % 500 == 0 )) && terminal_answered; then
        cancel; exit 0      # 终端先答：本钩子输出已不起作用，撤卡退出
    fi
    if [[ $WAITED_MS -lt 5000 ]]; then
        sleep 0.1; WAITED_MS=$((WAITED_MS + 100))
    else
        sleep 0.25; WAITED_MS=$((WAITED_MS + 250))
    fi
done

cancel    # 超时：不裁决，终端框照常；撤掉岛上的卡
exit 0
