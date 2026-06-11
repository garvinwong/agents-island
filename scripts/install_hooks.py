#!/usr/bin/env python3
"""Agents Island — Claude Code（及其分支 CLI）hooks 安装器（WSL 内执行）

把审批/通知 hook 合并写入目标 settings.json（只追加，不覆盖已有 hooks；
若同事件已存在指向本仓库或 agent-monitor 的同名脚本则跳过）。

用法:
  python3 scripts/install_hooks.py                       # Claude Code 本体
  python3 scripts/install_hooks.py --agent qoder          # 预设分支 CLI
  python3 scripts/install_hooks.py --agent qwen --config ~/.qwen/settings.json
  python3 scripts/install_hooks.py --list                 # 列出预设
  附加: --dry 预览 / --uninstall 还原备份

Claude-fork 分支 CLI（Qoder / Qwen Code / Factory / CodeBuddy 等）hook 协议
与 Claude Code 同构，复用同一组脚本；通过命令前缀 ISLAND_AGENT_SOURCE=<agent>
打来源标记，岛 UI 数据驱动自动出现对应分组（兜底色+大写标签）。
注：分支 CLI 的 settings 路径为社区通行约定，本机未装时无法实测——装好后
跑一条工具调用验证岛上弹卡即可。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 预设：Claude Code 分支 CLI 的常见配置路径（hook 协议与 Claude 同构）
PRESETS = {
    'claude':    '~/.claude/settings.json',
    'qoder':     '~/.qoder/settings.json',
    'qwen':      '~/.qwen/settings.json',
    'factory':   '~/.factory/settings.json',
    'codebuddy': '~/.codebuddy/settings.json',
    'iflow':     '~/.iflow/settings.json',
}


def build_want(agent: str) -> dict:
    pre = REPO / 'hooks' / 'pre_tool_use.sh'
    ntf = REPO / 'hooks' / 'notify_hook.sh'
    prefix = '' if agent == 'claude' else f'ISLAND_AGENT_SOURCE={agent} '
    return {
        'PreToolUse':   f'{prefix}bash {pre}',
        'Stop':         f'{prefix}bash {ntf}',
        'Notification': f'{prefix}bash {ntf}',
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--agent', default='claude', help='agent 标记（预设名或自定义）')
    ap.add_argument('--config', default=None, help='settings.json 路径（覆盖预设）')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args()

    if args.list:
        for k, v in PRESETS.items():
            print(f'  {k:10s} → {v}')
        return

    cfg = args.config or PRESETS.get(args.agent)
    if not cfg:
        sys.exit(f'❌ 未知 agent "{args.agent}" 且未给 --config；--list 查看预设')
    settings_path = Path(cfg).expanduser()
    backup = settings_path.with_suffix('.json.bak-island')

    if args.uninstall:
        if backup.exists():
            settings_path.write_text(backup.read_text(encoding='utf-8'), encoding='utf-8')
            print(f'✅ 已还原 {settings_path} ← {backup.name}')
        else:
            print('无备份，无需还原')
        return

    if not settings_path.parent.exists():
        sys.exit(f'❌ {settings_path.parent} 不存在——该 CLI 未安装或路径不同，'
                 f'用 --config 指定其 settings.json')

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            sys.exit(f'❌ {settings_path} 不是合法 JSON，请先手工修复')

    want = build_want(args.agent)
    hooks = settings.setdefault('hooks', {})
    changed = []
    for event, command in want.items():
        entries = hooks.setdefault(event, [])
        existing = [h.get('command', '')
                    for e in entries for h in e.get('hooks', [])]
        marker = command.split('/')[-1]
        if any(marker in c for c in existing):
            print(f'  ✓ {event}: 已有 {marker}，跳过')
            continue
        entries.append({'matcher': '',
                        'hooks': [{'type': 'command', 'command': command}]})
        changed.append(event)
        print(f'  + {event}: {command}')

    if not changed:
        print('无需改动。')
        return
    if args.dry:
        print('（--dry 预览模式，未写入）')
        return
    if settings_path.exists():
        backup.write_text(settings_path.read_text(encoding='utf-8'), encoding='utf-8')
        print(f'  备份 → {backup}')
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                             encoding='utf-8')
    print(f'✅ 已写入 {settings_path}（{", ".join(changed)}）。重启 {args.agent} 会话后生效。')


if __name__ == '__main__':
    main()
