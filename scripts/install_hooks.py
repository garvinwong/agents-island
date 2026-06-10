#!/usr/bin/env python3
"""Agents Island — Claude Code hooks 安装器（WSL 内执行）

把审批/通知 hook 合并写入 ~/.claude/settings.json（只追加，不覆盖已有 hooks；
若同事件已存在指向本仓库或 agent-monitor 的同名脚本则跳过）。
用法:  python3 scripts/install_hooks.py          # 安装
       python3 scripts/install_hooks.py --dry    # 只预览不写入
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / '.claude' / 'settings.json'

WANT = {
    'PreToolUse':   str(REPO / 'hooks' / 'pre_tool_use.sh'),
    'Stop':         str(REPO / 'hooks' / 'notify_hook.sh'),
    'Notification': str(REPO / 'hooks' / 'notify_hook.sh'),
}


def main():
    dry = '--dry' in sys.argv
    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            sys.exit(f'❌ {SETTINGS} 不是合法 JSON，请先手工修复')

    hooks = settings.setdefault('hooks', {})
    changed = []
    for event, script in WANT.items():
        entries = hooks.setdefault(event, [])
        existing = [h.get('command', '')
                    for e in entries for h in e.get('hooks', [])]
        marker = Path(script).name
        if any(marker in c for c in existing):
            print(f'  ✓ {event}: 已有 {marker}，跳过')
            continue
        entries.append({'matcher': '',
                        'hooks': [{'type': 'command', 'command': script}]})
        changed.append(event)
        print(f'  + {event}: {script}')

    if not changed:
        print('无需改动。')
        return
    if dry:
        print('（--dry 预览模式，未写入）')
        return
    backup = SETTINGS.with_suffix('.json.bak')
    if SETTINGS.exists():
        backup.write_text(SETTINGS.read_text(encoding='utf-8'), encoding='utf-8')
        print(f'  备份 → {backup}')
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                        encoding='utf-8')
    print(f'✅ 已写入 {SETTINGS}（{", ".join(changed)}）。重启 Claude Code 会话后生效。')


if __name__ == '__main__':
    main()
