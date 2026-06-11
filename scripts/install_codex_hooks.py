#!/usr/bin/env python3
"""安装 Agents Island 的 Codex CLI hooks（~/.codex/hooks.json）。

写入三个事件：
  PreToolUse        审批 Bash（Codex 该事件只覆盖 shell 工具）
  PermissionRequest 审批 exec/patch（编辑确认等终端提问，0.139+ 新事件）
  Stop              完成通知 + 清 Always 标志

幂等；--dry 预览；--uninstall 还原备份。改前备份 hooks.json.bak-island。
⚠️ Codex 在 config.toml [hooks.state] 按内容指纹记录 hook 信任——改动后
新开 Codex 会话可能提示确认信任一次，属预期。
"""
import json
import sys
from pathlib import Path

REPO  = Path(__file__).resolve().parent.parent
HOOKS_FILE = Path.home() / '.codex' / 'hooks.json'
BACKUP     = HOOKS_FILE.with_name('hooks.json.bak-island')

PRE  = str(REPO / 'hooks' / 'codex_pre_tool_use.sh')
PERM = str(REPO / 'hooks' / 'codex_permission_request.sh')
NTF  = str(REPO / 'hooks' / 'codex_notify_hook.sh')

DESIRED = {
    'PreToolUse': [{
        'matcher': '',
        'hooks': [{'type': 'command', 'command': PRE, 'timeout': 40,
                   'statusMessage': 'Agents Island 审批 Bash 命令'}],
    }],
    'PermissionRequest': [{
        'matcher': '',
        'hooks': [{'type': 'command', 'command': PERM, 'timeout': 40,
                   'statusMessage': 'Agents Island 审批 exec/patch'}],
    }],
    'Stop': [{
        'hooks': [{'type': 'command', 'command': NTF, 'timeout': 10}],
    }],
}


def main():
    dry = '--dry' in sys.argv
    uninstall = '--uninstall' in sys.argv

    if uninstall:
        if BACKUP.exists():
            HOOKS_FILE.write_text(BACKUP.read_text(encoding='utf-8'), encoding='utf-8')
            print(f'✅ 已还原 {HOOKS_FILE} ← {BACKUP.name}')
        else:
            print('无备份，无需还原')
        return

    cur = {}
    if HOOKS_FILE.exists():
        try:
            cur = json.loads(HOOKS_FILE.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            print('❌ 现有 hooks.json 解析失败，请人工检查')
            sys.exit(1)

    hooks = cur.get('hooks', {})
    if all(json.dumps(hooks.get(k)) == json.dumps(v) for k, v in DESIRED.items()):
        print('  ✓ 已安装，跳过')
        return

    new = {'hooks': {**hooks, **DESIRED}}
    print(f'  写入 hooks（PreToolUse / PermissionRequest / Stop）→ {HOOKS_FILE}')
    for k in DESIRED:
        print(f'    {k}: {DESIRED[k][0]["hooks"][0]["command"]}')
    if dry:
        print('（--dry 预览，未写入）')
        return
    if HOOKS_FILE.exists():
        BACKUP.write_text(HOOKS_FILE.read_text(encoding='utf-8'), encoding='utf-8')
    HOOKS_FILE.write_text(json.dumps(new, ensure_ascii=False, indent=2),
                          encoding='utf-8')
    print(f'✅ 已安装（备份 {BACKUP.name}）。新开 Codex 会话生效，'
          '首次可能提示确认 hook 信任。')


if __name__ == '__main__':
    main()
