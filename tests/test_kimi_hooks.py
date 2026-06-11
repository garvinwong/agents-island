#!/usr/bin/env python3
"""Kimi CLI hooks 沙箱测试（不触碰真实 /tmp 队列与 ~/.kimi 配置）。

运行：cd apps/agents-island && python3 -m pytest tests/test_kimi_hooks.py -v
覆盖：deny+reason 透传 / allow 静默放行 / always 标志秒放行 / agent_source 标识 / notify 入队。
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
PRE = HOOKS / 'kimi_pre_tool_use.sh'
NTF = HOOKS / 'kimi_notify_hook.sh'


@pytest.fixture()
def sandbox(tmp_path):
    env = dict(os.environ,
               ISLAND_QUEUE_FILE=str(tmp_path / 'q.jsonl'),
               ISLAND_RESP_DIR=str(tmp_path / 'resp'),
               ISLAND_ALWAYS_KIMI=str(tmp_path / 'always'))
    (tmp_path / 'resp').mkdir()
    return tmp_path, env


def _run_hook(script, stdin_obj, env, timeout=45):
    return subprocess.run(['bash', str(script)], input=json.dumps(stdin_obj),
                          capture_output=True, text=True, env=env, timeout=timeout)


def _respond_when_queued(tmp, decision, reason=''):
    """后台线程：等队列出现条目后写响应文件（模拟岛上点击）。"""
    def worker():
        q = tmp / 'q.jsonl'
        for _ in range(80):
            if q.exists() and q.read_text().strip():
                entry = json.loads(q.read_text().strip().splitlines()[-1])
                resp = {'decision': decision}
                if reason:
                    resp['reason'] = reason
                (tmp / 'resp' / f"{entry['id']}.json").write_text(
                    json.dumps(resp, ensure_ascii=False))
                return
            time.sleep(0.25)
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def test_deny_reason_passthrough(sandbox):
    tmp, env = sandbox
    _respond_when_queued(tmp, 'deny', '测试拒绝理由')
    r = _run_hook(PRE, {'hook_event_name': 'PreToolUse', 'session_id': 's1',
                        'cwd': '/tmp', 'tool_name': 'shell',
                        'tool_input': {'command': 'echo hi'}}, env)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    h = out['hookSpecificOutput']
    assert h['permissionDecision'] == 'deny'
    assert '测试拒绝理由' in h['permissionDecisionReason']


def test_allow_silent(sandbox):
    tmp, env = sandbox
    _respond_when_queued(tmp, 'allow')
    r = _run_hook(PRE, {'hook_event_name': 'PreToolUse', 'session_id': 's1',
                        'tool_name': 'read_file', 'tool_input': {}}, env)
    assert r.returncode == 0
    assert r.stdout.strip() == ''     # 无输出 = Kimi 按 allow 处理


def test_always_flag_instant(sandbox):
    tmp, env = sandbox
    (tmp / 'always').write_text('{}')
    t0 = time.time()
    r = _run_hook(PRE, {'tool_name': 'shell', 'tool_input': {}}, env, timeout=10)
    assert r.returncode == 0 and r.stdout.strip() == ''
    assert time.time() - t0 < 5       # 不等待，直接放行
    assert not (tmp / 'q.jsonl').exists() or not (tmp / 'q.jsonl').read_text().strip()


def test_agent_source_tag(sandbox):
    tmp, env = sandbox
    _respond_when_queued(tmp, 'allow')
    _run_hook(PRE, {'tool_name': 'shell', 'tool_input': {}, 'session_id': 's9'}, env)
    entry = json.loads((tmp / 'q.jsonl').read_text().strip().splitlines()[-1])
    assert entry['agent_source'] == 'kimi'
    assert entry['session_id'] == 's9'
    assert entry['id']


def test_notify_enqueue_and_flag_clear(sandbox):
    tmp, env = sandbox
    (tmp / 'always').write_text('{}')
    r = _run_hook(NTF, {'hook_event_name': 'Stop', 'session_id': 's1'}, env, timeout=10)
    assert r.returncode == 0
    entry = json.loads((tmp / 'q.jsonl').read_text().strip().splitlines()[-1])
    assert entry['type'] == 'notify'
    assert entry['agent_source'] == 'kimi'
    assert not (tmp / 'always').exists()   # Stop 清除 Always 标志


# ── Codex PermissionRequest hook（同沙箱基建，附在本文件）──────────────
CODEX_PERM = HOOKS / 'codex_permission_request.sh'


def test_codex_perm_allow(sandbox):
    tmp, env = sandbox
    env = dict(env, ISLAND_ALWAYS_CODEX=str(tmp / 'always_cx'))
    _respond_when_queued(tmp, 'allow')
    r = _run_hook(CODEX_PERM, {'hook_event_name': 'PermissionRequest',
                               'session_id': 'cx1',
                               'changes': {'/tmp/x.py': {'kind': 'edit'}}}, env)
    assert r.returncode == 0
    out = json.loads(r.stdout)
    d = out['hookSpecificOutput']
    assert d['hookEventName'] == 'PermissionRequest'
    assert d['decision']['behavior'] == 'allow'
    entry = json.loads((tmp / 'q.jsonl').read_text().strip().splitlines()[-1])
    assert entry['agent_source'] == 'codex'
    assert entry['tool_name'] == 'ApplyPatch'   # 含 changes → 推断为补丁审批


def test_codex_perm_deny_with_reason(sandbox):
    tmp, env = sandbox
    env = dict(env, ISLAND_ALWAYS_CODEX=str(tmp / 'always_cx'))
    _respond_when_queued(tmp, 'deny', '岛上拒绝测试')
    r = _run_hook(CODEX_PERM, {'hook_event_name': 'PermissionRequest',
                               'command': 'echo x'}, env)
    out = json.loads(r.stdout)
    d = out['hookSpecificOutput']['decision']
    assert d['behavior'] == 'deny'
    assert d['message'] == '岛上拒绝测试'


# ── Claude-fork 分支 CLI 来源标记（ISLAND_AGENT_SOURCE）────────────────
CLAUDE_PRE = HOOKS / 'pre_tool_use.sh'
CLAUDE_NTF = HOOKS / 'notify_hook.sh'


def test_fork_source_tag(sandbox):
    tmp, env = sandbox
    env = dict(env, ISLAND_AGENT_SOURCE='qoder')
    _respond_when_queued(tmp, 'allow')
    r = _run_hook(CLAUDE_PRE, {'session_id': 'q1', 'tool_name': 'Bash',
                               'tool_input': {'command': 'echo hi'}}, env)
    assert r.returncode == 0
    entry = json.loads((tmp / 'q.jsonl').read_text().strip().splitlines()[-1])
    assert entry['agent_source'] == 'qoder'


def test_fork_notify_clears_own_flag(sandbox):
    tmp, env = sandbox
    flag = Path(env['ISLAND_STATE_DIR']) / 'always_qoder' if 'ISLAND_STATE_DIR' in env else tmp / 'always_qoder'
    env = dict(env, ISLAND_AGENT_SOURCE='qoder', ISLAND_STATE_DIR=str(tmp))
    (tmp / 'always_qoder').write_text('{}')
    r = _run_hook(CLAUDE_NTF, {'hook_event_name': 'Stop', 'session_id': 'q1'}, env, timeout=10)
    assert r.returncode == 0
    entry = json.loads((tmp / 'q.jsonl').read_text().strip().splitlines()[-1])
    assert entry['agent_source'] == 'qoder'
    assert not (tmp / 'always_qoder').exists()
