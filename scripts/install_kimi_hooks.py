#!/usr/bin/env python3
"""安装 Agents Island 的 Kimi CLI hooks（~/.kimi/config.toml）。

写入三条 hook：PreToolUse（审批上岛）、Stop / Notification（toast + 清 Always 标志）。
幂等；--dry 预览；--uninstall 还原。改前备份 config.toml.bak-island，
改后用 tomllib 校验整文件可解析，解析失败自动回滚。

注意：Kimi hooks 配置是顶层 `hooks = [...]` 数组（kimi-cli >= 1.45），
本脚本以「行级手术」改写该数组，不重排用户配置的其余部分。
"""
import re
import sys
import tomllib
from pathlib import Path

REPO    = Path(__file__).resolve().parent.parent
CONFIG  = Path.home() / '.kimi' / 'config.toml'
BACKUP  = CONFIG.with_name('config.toml.bak-island')
MARK    = 'agents-island'   # 幂等识别标记（hook 路径中天然包含）

PRE  = REPO / 'hooks' / 'kimi_pre_tool_use.sh'
NTF  = REPO / 'hooks' / 'kimi_notify_hook.sh'

HOOKS_BLOCK = f"""hooks = [
  {{ event = "PreToolUse", command = "bash {PRE}", matcher = "", timeout = 150 }},
  {{ event = "Stop", command = "bash {NTF}", matcher = "", timeout = 10 }},
  {{ event = "Notification", command = "bash {NTF}", matcher = "", timeout = 10 }},
]"""


def main():
    dry = '--dry' in sys.argv
    uninstall = '--uninstall' in sys.argv

    if not CONFIG.exists():
        print(f'❌ 未找到 {CONFIG}（Kimi CLI 未安装或未初始化）')
        sys.exit(1)
    text = CONFIG.read_text(encoding='utf-8')

    if uninstall:
        if MARK not in text:
            print('未安装，无需还原')
            return
        new = re.sub(r'hooks = \[\n(?:.*agents-island.*\n)+\]', 'hooks = []', text)
        _write_validated(new, dry, '已还原 hooks = []')
        return

    if MARK in text:
        print('  ✓ 已安装，跳过')
        return

    m = re.search(r'^hooks = \[\]', text, re.M)
    if not m:
        # 用户已有自定义 hooks：在数组闭合括号前追加我们的三条
        m2 = re.search(r'^hooks = \[\n(.*?)^\]', text, re.M | re.S)
        if not m2:
            print('❌ 未找到可识别的 hooks 配置（既无 hooks = [] 也无多行数组），请手工合并：')
            print(HOOKS_BLOCK)
            sys.exit(1)
        inner = HOOKS_BLOCK.split('\n', 1)[1].rsplit('\n', 1)[0]
        new = text[:m2.end(1)] + inner + '\n' + text[m2.end(1):]
    else:
        new = text[:m.start()] + HOOKS_BLOCK + text[m.end():]

    print('  写入 hooks（PreToolUse / Stop / Notification）→', CONFIG)
    _write_validated(new, dry, '✅ 已安装。新开 Kimi 会话生效。')


def _write_validated(new_text: str, dry: bool, ok_msg: str):
    try:
        parsed = tomllib.loads(new_text)
    except Exception as e:
        print(f'❌ 生成的 TOML 解析失败，未写入：{e}')
        sys.exit(1)
    hooks = parsed.get('hooks', [])
    for h in hooks:
        assert {'event', 'command'} <= set(h), f'hook 字段缺失: {h}'
    if dry:
        print('（--dry 预览，未写入）')
        print(HOOKS_BLOCK)
        return
    BACKUP.write_text(CONFIG.read_text(encoding='utf-8'), encoding='utf-8')
    CONFIG.write_text(new_text, encoding='utf-8')
    print(f'{ok_msg}（备份 {BACKUP.name}）')


if __name__ == '__main__':
    main()
