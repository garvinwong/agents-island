#!/usr/bin/env python3
"""kimi_monitor 会话归属测试（沙箱 sessions 树 + kimi.json，不碰真实 ~/.kimi）。

复现并锁死的 Bug：_find_live_session_info 曾无视 pid/cwd，永远取「全局最新」
的 wire.jsonl，导致多个并发 Kimi 会话被贴上同一个（最近写入的）会话身份，
岛上折叠成一个、其余会话消失。修复后须按进程 cwd 归属到各自会话。

运行：cd apps/agents-island && python3 -m pytest tests/test_kimi_monitor.py -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

VENDOR = Path(__file__).resolve().parent.parent / 'bridge' / 'vendor'
sys.path.insert(0, str(VENDOR))
import kimi_monitor as km  # noqa: E402


def _mk_session(sess_root: Path, wshash: str, sid: str, title: str, mtime: float):
    """在 sessions/<wshash>/<sid>/ 造一个含 TurnBegin+custom_title 的会话。"""
    d = sess_root / wshash / sid
    d.mkdir(parents=True)
    (d / 'wire.jsonl').write_text(
        json.dumps({'message': {'type': 'TurnBegin',
                                'payload': {'user_input': title}}}) + '\n',
        encoding='utf-8')
    (d / 'state.json').write_text(json.dumps({'custom_title': title}), encoding='utf-8')
    os.utime(d / 'wire.jsonl', (mtime, mtime))
    return d


@pytest.fixture()
def two_sessions(tmp_path, monkeypatch):
    """两个并发会话：A 在 work/a（较旧），B 在 work/b（全局最新）。"""
    sess = tmp_path / 'sessions'
    cfg = tmp_path / 'kimi.json'
    a_cwd = tmp_path / 'work' / 'a'
    b_cwd = tmp_path / 'work' / 'b'
    a_cwd.mkdir(parents=True)
    b_cwd.mkdir(parents=True)
    _mk_session(sess, 'hashA', 'uuid-a', '液态玻璃 UI 开发', mtime=1000)
    _mk_session(sess, 'hashB', 'uuid-b', '一封家书初稿', mtime=9999)  # 全局最新
    cfg.write_text(json.dumps({'work_dirs': [
        {'path': str(a_cwd), 'last_session_id': 'uuid-a'},
        {'path': str(b_cwd), 'last_session_id': 'uuid-b'},
    ]}), encoding='utf-8')
    monkeypatch.setattr(km, 'KIMI_CONFIG', cfg)
    monkeypatch.setattr(km, 'KIMI_SESS_DIR', sess)
    return tmp_path, str(a_cwd), str(b_cwd)


def test_attributes_by_cwd_not_global_latest(two_sessions):
    """cwd=work/a 的进程必须拿到会话 A，而非全局最新的会话 B。"""
    _, a_cwd, _ = two_sessions
    sid, slug, _tool, _ctx = km._find_live_session_info('101', a_cwd)
    assert sid == 'uuid-a', f'期望归属 uuid-a，实得 {sid}'
    assert '液态玻璃' in (slug or '')


def test_two_processes_get_distinct_sessions(two_sessions):
    """两个不同 cwd 的进程必须各自拿到不同会话身份（不再折叠）。"""
    _, a_cwd, b_cwd = two_sessions
    sid_a, _, _, _ = km._find_live_session_info('101', a_cwd)
    sid_b, _, _, _ = km._find_live_session_info('102', b_cwd)
    assert sid_a == 'uuid-a'
    assert sid_b == 'uuid-b'
    assert sid_a != sid_b


def test_cwd_realpath_alias(two_sessions, tmp_path):
    """进程 cwd 经软链（/mnt/*↔ext4 桥）指向 work/a 时，仍应 realpath 归一命中 A。"""
    _, a_cwd, _ = two_sessions
    link = tmp_path / 'aliased_cwd'
    os.symlink(a_cwd, link)
    sid, _, _, _ = km._find_live_session_info('101', str(link))
    assert sid == 'uuid-a'


def test_unmapped_cwd_does_not_borrow_other_session(two_sessions, tmp_path):
    """cwd 在 kimi.json 无映射时，不得借用别的会话身份（宁可 None 也不错贴）。"""
    _, _, _ = two_sessions
    orphan = tmp_path / 'work' / 'orphan'
    orphan.mkdir(parents=True)
    sid, slug, _tool, _ctx = km._find_live_session_info('103', str(orphan))
    assert sid is None
    assert slug is None
