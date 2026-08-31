#!/usr/bin/env bash
# 岛上看图/看 demo/读文档：把本机文件弹成 Windows 独立查看窗口，免翻 \\wsl$ 目录
# 链路: 本脚本 → 桥 POST /api/show → 岛页面轮询 → IslandApi.show_content 弹窗
# 用法: bash show.sh <文件路径> [--kind image|html|pdf|md] [--raw]
#   kind 不传按扩展名判断；--raw 跳过曜石壳直开原文件（壳不兼容时的兜底）
set -euo pipefail

PORT="${ISLAND_PORT:-5599}"
FILE="${1:-}"
KIND=""
RAW=false
if [ -z "$FILE" ]; then
    echo "用法: show.sh <文件路径> [--kind image|html|pdf|md] [--raw]" >&2
    exit 2
fi
shift
while [ $# -gt 0 ]; do
    case "$1" in
        --kind) KIND="${2:-}"; shift 2 ;;
        --raw)  RAW=true; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

FILE="$(realpath "$FILE")"
if [ ! -f "$FILE" ]; then
    echo "文件不存在: $FILE" >&2
    exit 2
fi

if [ -z "$KIND" ]; then
    ext="${FILE##*.}"
    case "${ext,,}" in
        png|jpg|jpeg|gif|webp|bmp|svg) KIND=image ;;
        html|htm)                      KIND=html ;;
        pdf)                           KIND=pdf ;;
        md|markdown)                   KIND=md ;;
        *) echo "无法从扩展名 .$ext 判断类型，请加 --kind image|html|pdf|md" >&2; exit 2 ;;
    esac
fi

# 岛壳在 Windows 侧，必须喂 Windows 路径（/mnt/<盘>→盘符、WSL ext4→\\wsl.localhost UNC）
WIN_PATH="$(wslpath -w "$FILE")"

# 路径可能含空格/引号，JSON 一律交给 python3 拼，不手写
BODY="$(python3 -c 'import json,sys; print(json.dumps(
    {"kind": sys.argv[1], "path": sys.argv[2], "win_path": sys.argv[3],
     "raw": sys.argv[4] == "true"}))' \
    "$KIND" "$FILE" "$WIN_PATH" "$RAW")"

RESP="$(curl -s -m 3 -X POST "http://127.0.0.1:${PORT}/api/show" -d "$BODY" || true)"
case "$RESP" in
    *'"ok": true'*|*'"ok":true'*)
        echo "已推送岛上查看窗口 [$KIND]: $FILE" ;;
    '')
        echo "岛桥未运行（127.0.0.1:${PORT} 无响应）。先 bash launch/start_bridge.sh，并确认岛壳（AgentsIsland）已启动" >&2
        exit 1 ;;
    *)
        echo "推送失败: $RESP" >&2
        exit 1 ;;
esac
