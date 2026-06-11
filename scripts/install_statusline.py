#!/usr/bin/env python3
"""安装 Agents Island statusLine 包装（用量追踪数据源）。

把 ~/.claude/settings.json 的 statusLine 命令替换为 island_statusline.sh，
原命令存入 hooks/statusline_delegate.txt 继续转发执行（HUD 显示不变）。
幂等；--dry 预览；--uninstall 还原。
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / '.claude' / 'settings.json'
WRAPPER = str(REPO / 'hooks' / 'island_statusline.sh')
DELEGATE_FILE = REPO / 'hooks' / 'statusline_delegate.txt'


def main():
    dry = '--dry' in sys.argv
    uninstall = '--uninstall' in sys.argv
    settings = json.loads(SETTINGS.read_text(encoding='utf-8')) if SETTINGS.exists() else {}
    cur = settings.get('statusLine', {})
    cur_cmd = cur.get('command', '') if isinstance(cur, dict) else ''

    if uninstall:
        if WRAPPER in cur_cmd and DELEGATE_FILE.exists():
            orig = DELEGATE_FILE.read_text(encoding='utf-8').strip()
            settings['statusLine'] = {'type': 'command', 'command': orig} if orig else None
            if not orig:
                settings.pop('statusLine', None)
            SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'✅ 已还原 statusLine -> {orig or "(无)"}')
        else:
            print('未安装，无需还原')
        return

    if WRAPPER in cur_cmd:
        print('  ✓ 已安装，跳过')
        return

    print(f'  原 statusLine: {cur_cmd or "(无)"}')
    print(f'  新 statusLine: bash {WRAPPER}')
    if dry:
        print('（--dry 预览，未写入）')
        return

    DELEGATE_FILE.write_text((cur_cmd or '') + '\n', encoding='utf-8')
    backup = SETTINGS.with_suffix('.json.bak-statusline')
    backup.write_text(SETTINGS.read_text(encoding='utf-8'), encoding='utf-8')
    settings['statusLine'] = {'type': 'command', 'command': f'bash {WRAPPER}'}
    SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'✅ 已安装（原命令转为 delegate，备份 {backup.name}）。新会话生效。')


if __name__ == '__main__':
    main()
