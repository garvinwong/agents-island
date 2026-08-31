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


# ── install_kimi_hooks.py：装钩子同时确保 default_yolo=true（沙箱 config）──
# 背景见 设计笔记「Kimi YOLO 与岛闸」：岛是唯一审批口的前提就是
# yolo=true；Kimi 每次更新会把 config.toml 刷回 yolo=false，此加固令重跑装钩子即修复。
import tomllib  # noqa: E402

INSTALLER = Path(__file__).resolve().parent.parent / 'scripts' / 'install_kimi_hooks.py'


def _run_installer(cfg_path, env, *args):
    return subprocess.run(
        ['python3', str(INSTALLER), *args],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ, ISLAND_KIMI_CONFIG=str(cfg_path)))


def _fresh_config(tmp, yolo_line='default_yolo = false'):
    cfg = tmp / 'config.toml'
    cfg.write_text(
        f'default_model = "x"\n{yolo_line}\ntheme = "dark"\nhooks = []\n',
        encoding='utf-8')
    return cfg


def test_installer_flips_yolo_false_to_true(tmp_path):
    cfg = _fresh_config(tmp_path, 'default_yolo = false')
    r = _run_installer(cfg, os.environ)
    assert r.returncode == 0, r.stderr + r.stdout
    parsed = tomllib.loads(cfg.read_text(encoding='utf-8'))
    assert parsed['default_yolo'] is True          # 已翻正
    # 钩子也照常装上
    assert any('agents-island' in h['command'] for h in parsed['hooks'])


def test_installer_inserts_yolo_when_absent(tmp_path):
    cfg = tmp_path / 'config.toml'
    cfg.write_text('default_model = "x"\ntheme = "dark"\nhooks = []\n', encoding='utf-8')
    r = _run_installer(cfg, os.environ)
    assert r.returncode == 0, r.stderr + r.stdout
    parsed = tomllib.loads(cfg.read_text(encoding='utf-8'))
    assert parsed['default_yolo'] is True           # 原本缺失→补上


def test_installer_yolo_idempotent(tmp_path):
    cfg = _fresh_config(tmp_path, 'default_yolo = true')
    r1 = _run_installer(cfg, os.environ)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    text1 = cfg.read_text(encoding='utf-8')
    r2 = _run_installer(cfg, os.environ)             # 再跑一次
    assert r2.returncode == 0, r2.stderr + r2.stdout
    parsed = tomllib.loads(cfg.read_text(encoding='utf-8'))
    assert parsed['default_yolo'] is True
    assert sum('agents-island' in h['command'] for h in parsed['hooks']) == 3  # 无重复
    # yolo 已 true 时不重复改动（幂等）
    assert cfg.read_text(encoding='utf-8').count('default_yolo') == 1


# ── 新版 Kimi Code（>=0.27，~/.kimi-code/config.toml）兼容 ──────────────
# 新版用 default_permission_mode = "yolo" 取代 default_yolo；hooks 为 [[hooks]]
# 表数组格式（迁移器产物）。安装器按路径含 .kimi-code 识别新版。
# 背景：2026-07-30 Owner 报 bash/write 审批不上岛——新版内建终端闸门在
# PreToolUse hook 之前拦截，必须 permission_mode=yolo 让内建闸让位于岛。

def _fresh_kimi_code_config(tmp, body=''):
    d = tmp / '.kimi-code'
    d.mkdir(exist_ok=True)
    cfg = d / 'config.toml'
    cfg.write_text(
        'default_model = "kimi-code/k3"\ntelemetry = true\n'
        '\n[loop_control]\nmax_retries_per_step = 3\n' + body,
        encoding='utf-8')
    return cfg


KIMI_CODE_HOOKS = '''
[[hooks]]
event = "PreToolUse"
matcher = ""
command = "bash /x/agents-island/hooks/kimi_pre_tool_use.sh"
timeout = 150

[[hooks]]
event = "Stop"
matcher = ""
command = "bash /x/agents-island/hooks/kimi_notify_hook.sh"
timeout = 10
'''


def test_installer_kimi_code_fresh(tmp_path):
    """新版空配置：装 hooks（表数组追加）+ 设 default_permission_mode=yolo，不碰 default_yolo。"""
    cfg = _fresh_kimi_code_config(tmp_path)
    r = _run_installer(cfg, os.environ)
    assert r.returncode == 0, r.stderr + r.stdout
    parsed = tomllib.loads(cfg.read_text(encoding='utf-8'))
    assert parsed.get('default_permission_mode') == 'yolo'
    assert 'default_yolo' not in parsed              # 新版不认这个键，不能写
    hooks = parsed.get('hooks', [])
    assert sum('agents-island' in h['command'] for h in hooks) == 3
    events = {h['event'] for h in hooks}
    assert events == {'PreToolUse', 'Stop', 'Notification'}


def test_installer_kimi_code_hooks_already_migrated(tmp_path):
    """迁移器已带来 [[hooks]]（含 agents-island）：只补 permission_mode，不重复装。"""
    cfg = _fresh_kimi_code_config(tmp_path, KIMI_CODE_HOOKS)
    r = _run_installer(cfg, os.environ)
    assert r.returncode == 0, r.stderr + r.stdout
    parsed = tomllib.loads(cfg.read_text(encoding='utf-8'))
    assert parsed.get('default_permission_mode') == 'yolo'
    assert sum('agents-island' in h['command'] for h in parsed['hooks']) == 2  # 原样，无重复


def test_installer_kimi_code_flips_mode(tmp_path):
    """default_permission_mode 已存在但非 yolo（如 manual/auto）→ 翻成 yolo。"""
    cfg = _fresh_kimi_code_config(tmp_path)
    cfg.write_text('default_permission_mode = "manual"\n' + cfg.read_text(encoding='utf-8'),
                   encoding='utf-8')
    r = _run_installer(cfg, os.environ)
    assert r.returncode == 0, r.stderr + r.stdout
    text = cfg.read_text(encoding='utf-8')
    parsed = tomllib.loads(text)
    assert parsed['default_permission_mode'] == 'yolo'
    assert text.count('default_permission_mode') == 1


def test_installer_kimi_code_idempotent(tmp_path):
    cfg = _fresh_kimi_code_config(tmp_path)
    r1 = _run_installer(cfg, os.environ)
    assert r1.returncode == 0, r1.stderr + r1.stdout
    text1 = cfg.read_text(encoding='utf-8')
    r2 = _run_installer(cfg, os.environ)
    assert r2.returncode == 0, r2.stderr + r2.stdout
    assert cfg.read_text(encoding='utf-8') == text1   # 第二次零改动
    parsed = tomllib.loads(text1)
    assert sum('agents-island' in h['command'] for h in parsed['hooks']) == 3
    assert text1.count('default_permission_mode') == 1
