#!/usr/bin/env python3
"""claude_monitor 会话标题 / 元数据测试（沙箱 jsonl，不碰真实 ~/.claude）。

复现并锁死的 Bug（2026-09-26 实测：5 个在线会话里 3 个标题一模一样）：
  ① 标题取「第一条用户消息」，斜杠命令留下的 <local-command-caveat> 系统文本
     排在最前 → 面板显示 "<local-command-caveat>Caveat: The messag…"；
     Claude Code 其实每轮都写 ai-title（终端标签页显示的也是它）。
  ② 分支读 git_branch 键，而 Claude Code 写的是 gitBranch → 永远显示 main。
  ③ 项目名由目录 slug 还原，"my-Workspace" 里的连字符被当路径分隔 → "Workspace"。
  ④ 会话无 custom-title 时每 8 秒全文重扫（7 天内会话记录合计数百 MB）。

运行：cd apps/agents-island && python3 -m pytest tests/test_claude_monitor.py -v
"""
import json
import sys
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parent.parent / 'bridge' / 'vendor'
sys.path.insert(0, str(VENDOR))
import claude_monitor as cm  # noqa: E402

CWD = '/home/user/wt/island-opt'
SLUG = '-home-user-wt-island-opt'


def _user(text, meta=False):
    o = {'type': 'user', 'cwd': CWD, 'gitBranch': 'feat/island-opt',
         'message': {'role': 'user', 'content': text}}
    if meta:
        o['isMeta'] = True
    return o


def _assistant(text='好的'):
    return {'type': 'assistant', 'cwd': CWD, 'gitBranch': 'feat/island-opt',
            'message': {'role': 'assistant', 'stop_reason': 'end_turn',
                        'content': [{'type': 'text', 'text': text}]}}


def _write(path: Path, objs, mode='w'):
    with open(path, mode, encoding='utf-8') as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False, separators=(',', ':')) + '\n')


@pytest.fixture
def sess_file(tmp_path, monkeypatch):
    d = tmp_path / SLUG
    d.mkdir()
    monkeypatch.setattr(cm, 'CLAUDE_DIR', tmp_path)
    cm._META_CACHE.clear() if hasattr(cm, '_META_CACHE') else None
    return d / 'a1b2c3d4-0000-0000-0000-000000000000.jsonl'


def _parse(f):
    return cm._parse_session(f, f.parent.name)


def test_title_prefers_ai_title_over_command_caveat(sess_file):
    _write(sess_file, [
        _user('<local-command-caveat>Caveat: The messages below were generated '
              'by the user while running local commands.</local-command-caveat>', meta=True),
        _user('<command-name>/model</command-name>\n<command-message>model</command-message>'),
        _user('<local-command-stdout>Set model to opus</local-command-stdout>'),
        _user('定位到灵动岛项目'),
        _assistant(),
        {'type': 'ai-title', 'aiTitle': '灵动岛项目优化', 'sessionId': 'x'},
        {'type': 'last-prompt', 'lastPrompt': '按你推荐的顺序做', 'sessionId': 'x'},
    ])
    s = _parse(sess_file)
    assert s['title'] == '灵动岛项目优化'
    assert s['last_prompt'] == '按你推荐的顺序做'


def test_title_falls_back_to_first_real_prompt(sess_file):
    """尚无 ai-title（新会话首轮）时，跳过系统注入的命令文本取真实首问。"""
    _write(sess_file, [
        _user('<local-command-caveat>Caveat: x</local-command-caveat>', meta=True),
        _user('<command-name>/clear</command-name>'),
        _user([{'type': 'text', 'text': '帮我看一下\n灵动岛的日志'}]),
        _assistant(),
    ])
    assert _parse(sess_file)['title'] == '帮我看一下 灵动岛的日志'


def test_custom_title_wins_and_latest_ai_title_used(sess_file):
    _write(sess_file, [_user('首问'), _assistant(),
                       {'type': 'ai-title', 'aiTitle': '旧标题'}])
    assert _parse(sess_file)['title'] == '旧标题'
    # 追加：新 ai-title 覆盖旧的（增量读取也要看到）
    _write(sess_file, [{'type': 'ai-title', 'aiTitle': '新标题'}], mode='a')
    assert _parse(sess_file)['title'] == '新标题'
    # /rename 的 custom-title 优先于 ai-title
    _write(sess_file, [{'type': 'custom-title', 'customTitle': '我起的名'}], mode='a')
    assert _parse(sess_file)['title'] == '我起的名'


def test_branch_and_project_from_cwd(sess_file):
    _write(sess_file, [_user('首问'), _assistant()])
    s = _parse(sess_file)
    assert s['git_branch'] == 'feat/island-opt'
    assert s['project'] == 'island-opt'


def test_project_name_keeps_hyphen(tmp_path, monkeypatch):
    """my-Workspace 这类带连字符的目录名不得被截成 Workspace。"""
    d = tmp_path / '-home-user-my-Workspace'
    d.mkdir()
    monkeypatch.setattr(cm, 'CLAUDE_DIR', tmp_path)
    f = d / 'b1.jsonl'
    o, a = _user('首问'), _assistant()
    o['cwd'] = a['cwd'] = '/home/user/my-Workspace'
    _write(f, [o, a])
    assert _parse(f)['project'] == 'my-Workspace'


def test_half_written_line_not_lost(sess_file):
    """写到一半的末行不得被吞：补完后下次扫描必须读到。"""
    _write(sess_file, [_user('首问'), _assistant()])
    _parse(sess_file)
    with open(sess_file, 'a', encoding='utf-8') as f:
        f.write('{"type":"ai-title","aiTi')          # 写到一半
    assert _parse(sess_file)['title'] == '首问'
    with open(sess_file, 'a', encoding='utf-8') as f:
        f.write('tle":"补完的标题"}\n')
    assert _parse(sess_file)['title'] == '补完的标题'


def test_unchanged_file_not_reread(sess_file, monkeypatch):
    """文件没变就不再读正文（此前无 custom-title 的会话每 8 秒全文重扫）。"""
    _write(sess_file, [_user('首问'), _assistant(),
                       {'type': 'ai-title', 'aiTitle': 'T'}])
    _parse(sess_file)
    reads = []
    real_open = open

    def spy_open(path, mode='r', *a, **kw):
        if str(path) == str(sess_file) and 'b' in mode:
            reads.append(mode)
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr('builtins.open', spy_open)
    cm._scan_meta(sess_file, sess_file.stat())
    assert reads == []
