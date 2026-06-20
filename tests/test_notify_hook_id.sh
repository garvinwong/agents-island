#!/usr/bin/env bash
# 复现/回归测试：notify_hook.sh 必须为每次事件生成唯一 id（非 notify_unknown）
# Bug：PERM_ID 曾作为 python argv 后缀传入，os.environ 取不到 → 所有 notify
#      退化成同一个 id notify_unknown → bridge 按 id 去重后只弹首条、其余静默丢弃。
set -u
HOOK="$(cd "$(dirname "$0")/.." && pwd)/hooks/notify_hook.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export ISLAND_STATE_DIR="$TMP"

# 跑两次（模拟两个会话先后结束），各喂一份不同的 Stop 事件
echo '{"hook_event_name":"Stop","session_id":"AAA"}' | bash "$HOOK"
echo '{"hook_event_name":"Stop","session_id":"BBB"}' | bash "$HOOK"

mapfile -t IDS < <(python3 -c "
import json
for line in open('$TMP/queue.jsonl'):
    try: print(json.loads(line)['id'])
    except: pass
")

fail=0
[ "${#IDS[@]}" -eq 2 ] || { echo \"FAIL: 期望 2 条，实际 ${#IDS[@]}\"; fail=1; }
for id in "${IDS[@]}"; do
  case "$id" in
    notify_unknown) echo "FAIL: id 退化为 notify_unknown"; fail=1;;
    notify_*) ;;  # ok
    *) echo "FAIL: id 前缀异常: $id"; fail=1;;
  esac
done
if [ "${#IDS[@]}" -eq 2 ] && [ "${IDS[0]}" = "${IDS[1]}" ]; then
  echo "FAIL: 两次事件 id 相同（${IDS[0]}）→ 会被 bridge 去重丢弃"; fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "PASS: 两条 notify id 唯一且非 notify_unknown: ${IDS[*]}"
else
  echo "ids = ${IDS[*]}"
fi
exit "$fail"
