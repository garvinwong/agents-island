#!/usr/bin/env python3
"""安装 Agents Island 的 Kimi hooks（新旧两代 CLI 通吃）。

写入三条 hook：PreToolUse（审批上岛）、Stop / Notification（toast + 清 Always 标志），
并确保内建审批闸全开——岛要成为唯一审批口（否则岛上放行后终端仍二次追问，
或高危工具卡在终端根本不上岛；详见 设计笔记「Kimi YOLO 与岛闸」）。

两代 CLI 差异（按 config 路径含 .kimi-code 识别新版）：
  - 旧版 kimi-cli（~/.kimi/config.toml）  ：hooks = [...] 内联数组；default_yolo = true
  - 新版 Kimi Code >=0.27（~/.kimi-code/config.toml）：[[hooks]] 表数组；
    default_permission_mode = "yolo"（迁移器只在旧 default_yolo=true 时转换，
    缺失则内建终端闸复活→bash/write 等高危审批不上岛，2026-07-30 实案）

无 ISLAND_KIMI_CONFIG 覆盖时自动选路：优先新版（存在即用），否则旧版。
Kimi 每次更新/迁移可能把权限模式刷回默认，重跑本脚本即修复。
幂等；--dry 预览；--uninstall 还原（仅撤 hooks，不动权限模式，避免意外收紧）。
改前备份 config.toml.bak-island，改后用 tomllib 校验整文件可解析，解析失败不写入。
沙箱测试可用环境变量 ISLAND_KIMI_CONFIG 覆盖目标 config 路径。
"""
import os
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARK = 'agents-island'   # 幂等识别标记（hook 路径中天然包含）

PRE = REPO / 'hooks' / 'kimi_pre_tool_use.sh'
NTF = REPO / 'hooks' / 'kimi_notify_hook.sh'

HOOK_DEFS = [
    ('PreToolUse', PRE, 150),
    ('Stop', NTF, 10),
    ('Notification', NTF, 10),
]

# 旧版：内联数组元素
HOOKS_BLOCK = 'hooks = [\n' + '\n'.join(
    f'  {{ event = "{ev}", command = "bash {cmd}", matcher = "", timeout = {to} }},'
    for ev, cmd, to in HOOK_DEFS) + '\n]'

# 新版：[[hooks]] 表数组（与迁移器产物同构）
HOOKS_TABLES = '\n'.join(
    f'\n[[hooks]]\nevent = "{ev}"\nmatcher = ""\ncommand = "bash {cmd}"\ntimeout = {to}\n'
    for ev, cmd, to in HOOK_DEFS)


def resolve_config() -> Path:
    env = os.environ.get('ISLAND_KIMI_CONFIG')
    if env:
        return Path(env)
    new = Path.home() / '.kimi-code' / 'config.toml'
    if new.exists():
        return new
    return Path.home() / '.kimi' / 'config.toml'


CONFIG = resolve_config()
BACKUP = CONFIG.with_name('config.toml.bak-island')
IS_KIMI_CODE = '.kimi-code' in str(CONFIG)


def main():
    dry = '--dry' in sys.argv
    uninstall = '--uninstall' in sys.argv

    if not CONFIG.exists():
        print(f'❌ 未找到 {CONFIG}（Kimi CLI 未安装或未初始化）')
        sys.exit(1)
    text = CONFIG.read_text(encoding='utf-8')
    flavor = 'Kimi Code(新版)' if IS_KIMI_CODE else 'kimi-cli(旧版)'
    print(f'  目标：{CONFIG} [{flavor}]')

    if uninstall:
        if MARK not in text:
            print('未安装，无需还原')
            return
        new = re.sub(r'hooks = \[\n(?:.*agents-island.*\n)+\]', 'hooks = []', text)
        new = _strip_hook_tables(new)
        _write_validated(new, dry, '已还原（撤除 agents-island hooks）')
        return

    # ── 1) 装 hooks（已装则跳过此步，但仍继续确保权限模式）──────────────
    if MARK in text:
        print('  ✓ hooks 已安装，跳过')
        new = text
    else:
        m = re.search(r'^hooks = \[\]', text, re.M)
        m2 = re.search(r'^hooks = \[\n(.*?)^\]', text, re.M | re.S)
        if m:
            new = text[:m.start()] + HOOKS_BLOCK + text[m.end():]
        elif m2:
            # 用户已有自定义内联 hooks：在数组闭合括号前追加我们的三条
            inner = HOOKS_BLOCK.split('\n', 1)[1].rsplit('\n', 1)[0]
            new = text[:m2.end(1)] + inner + '\n' + text[m2.end(1):]
        else:
            # 无内联数组（新版常态）：文件末尾追加 [[hooks]] 表数组
            new = text.rstrip('\n') + '\n' + HOOKS_TABLES
        print('  写入 hooks（PreToolUse / Stop / Notification）→', CONFIG)

    # ── 2) 确保内建审批闸让位于岛 ─────────────────────────────────────
    if IS_KIMI_CODE:
        new, changed = _ensure_permission_mode_yolo(new)
        if changed:
            print('  设 default_permission_mode="yolo"（内建终端闸让位于岛；'
                  'Kimi 更新后复发重跑本脚本即修复）')
    else:
        new, changed = _ensure_yolo(new)
        if changed:
            print('  设 default_yolo=true（内建审批让位于岛）')

    if new == text:
        print('  ✓ 无需改动（hooks 已装 + 权限模式已就位）')
        return
    _write_validated(new, dry, '✅ 已安装。新开 Kimi 会话生效。')


def _ensure_yolo(text: str) -> tuple[str, bool]:
    """旧版：确保顶层 default_yolo=true。false→翻正；已 true→不动；缺失→顶部插入。"""
    m = re.search(r'^(\s*default_yolo\s*=\s*)(true|false)\s*$', text, re.M)
    if m:
        if m.group(2) == 'true':
            return text, False
        return text[:m.start()] + m.group(1) + 'true' + text[m.end():], True
    return 'default_yolo = true\n' + text, True


def _ensure_permission_mode_yolo(text: str) -> tuple[str, bool]:
    """新版：确保顶层 default_permission_mode="yolo"。
    其他值（manual/auto）→翻成 yolo；已 yolo→不动；缺失→顶部插入（顶层裸键须在任何 [table] 之前）。"""
    m = re.search(r'^(\s*default_permission_mode\s*=\s*)"(\w+)"\s*$', text, re.M)
    if m:
        if m.group(2) == 'yolo':
            return text, False
        return text[:m.start()] + m.group(1) + '"yolo"' + text[m.end():], True
    return 'default_permission_mode = "yolo"\n' + text, True


def _strip_hook_tables(text: str) -> str:
    """撤除含 agents-island 的 [[hooks]] 表数组块（uninstall 用）。
    块 = [[hooks]] 行起，到下一个 [table] 头（或文件尾）为止。"""
    lines = text.splitlines(keepends=True)
    result, i = [], 0
    while i < len(lines):
        if lines[i].strip() == '[[hooks]]':
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith('['):
                j += 1
            block = lines[i:j]
            if not any(MARK in ln for ln in block):
                result.extend(block)
            i = j
        else:
            result.append(lines[i])
            i += 1
    return ''.join(result)


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
        print(HOOKS_TABLES if IS_KIMI_CODE else HOOKS_BLOCK)
        return
    BACKUP.write_text(CONFIG.read_text(encoding='utf-8'), encoding='utf-8')
    CONFIG.write_text(new_text, encoding='utf-8')
    print(f'{ok_msg}（备份 {BACKUP.name}）')


if __name__ == '__main__':
    main()
