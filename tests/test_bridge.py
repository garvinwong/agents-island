#!/usr/bin/env python3
"""island_bridge 单元/集成测试（隔离沙箱：不触碰真实 /tmp 队列）。

运行：cd apps/agents-island && python3 -m pytest tests/test_bridge.py -v
对应 RISKS.md 用例 T1~T3、T9、T11、T12。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

BRIDGE = Path(__file__).resolve().parent.parent / 'bridge' / 'island_bridge.py'
PORT = 5589  # 测试专用端口：避开生产 5599 与 SSH 隧道 5598


def _api(path, payload=None, method=None):
    url = f'http://127.0.0.1:{PORT}{path}'
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'))
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture(scope='module')
def bridge():
    """启动隔离沙箱 bridge 子进程。"""
    tmp = tempfile.mkdtemp(prefix='island_test_')
    queue = Path(tmp) / 'queue.jsonl'
    resp_dir = Path(tmp) / 'responses'
    env = dict(os.environ,
               ISLAND_QUEUE_FILE=str(queue),
               ISLAND_RESP_DIR=str(resp_dir),
               ISLAND_ALWAYS_CLAUDE=str(Path(tmp) / 'always_claude'),
               ISLAND_ALWAYS_CODEX=str(Path(tmp) / 'always_codex'),
               ISLAND_SETTINGS_FILE=str(Path(tmp) / 'settings.json'))
    proc = subprocess.Popen([sys.executable, str(BRIDGE), '--port', str(PORT), '--debug'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            code, _b = _api('/api/health')
            if code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail('bridge 未能启动')
    yield {'queue': queue, 'resp_dir': resp_dir, 'tmp': Path(tmp)}
    proc.kill()
    proc.wait()


def _enqueue(bridge, **kw):
    """直接写沙箱队列文件（模拟 hook 追加）。"""
    entry = {'id': kw.pop('id', f'u_{time.time_ns()}'), 'session_id': 'sess-t',
             'tool_name': 'Bash', 'tool_input': {'command': 'echo hi'}}
    entry.update(kw)
    with open(bridge['queue'], 'a') as f:
        f.write(json.dumps(entry) + '\n')
    return entry['id']


def _wait_pending(eid, present=True, timeout=4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _c, state = _api('/api/state')
        ids = [p['id'] for p in state['pending']]
        if (eid in ids) == present:
            return state
        time.sleep(0.2)
    pytest.fail(f'pending 等待超时: {eid} present={present}')


# ── T1: state 结构 ────────────────────────────────────────────────────
def test_state_shape(bridge):
    code, state = _api('/api/state')
    assert code == 200
    for key in ('pending', 'notify', 'sessions', 'ts'):
        assert key in state
    # 会话扫描线程就绪后应含全部已注册适配器键（首扫遍历全部 transcript，
    # 机器负载高时 8s 不够 → 20s；曾致套件 flaky）
    expected = {'claude', 'codex', 'agy', 'gemini', 'kimi'}
    deadline = time.time() + 20
    while time.time() < deadline:
        _c, state = _api('/api/state')
        if set(state['sessions'].keys()) == expected:
            break
        time.sleep(0.5)
    assert set(state['sessions'].keys()) == expected
    assert isinstance(state['sessions']['claude'], list)


# ── T2: allow 全链路 ──────────────────────────────────────────────────
def test_allow_flow(bridge):
    eid = _enqueue(bridge)
    _wait_pending(eid)
    code, body = _api('/api/decision', {'id': eid, 'decision': 'allow'})
    assert code == 200 and body['ok']
    resp = bridge['resp_dir'] / f'{eid}.json'
    assert resp.exists()
    assert json.loads(resp.read_text())['decision'] == 'allow'
    _wait_pending(eid, present=False)
    resp.unlink()


# ── T3a: deny ────────────────────────────────────────────────────────
def test_deny_flow(bridge):
    eid = _enqueue(bridge)
    _wait_pending(eid)
    code, body = _api('/api/decision', {'id': eid, 'decision': 'deny'})
    assert code == 200
    resp = bridge['resp_dir'] / f'{eid}.json'
    assert json.loads(resp.read_text())['decision'] == 'deny'
    resp.unlink()


# ── T3b: always 写标志 + 后续条目自动放行 ─────────────────────────────
def test_always_flow(bridge):
    flag = bridge['tmp'] / 'always_claude'
    eid = _enqueue(bridge)
    _wait_pending(eid)
    code, _b = _api('/api/decision', {'id': eid, 'decision': 'always'})
    assert code == 200
    assert json.loads((bridge['resp_dir'] / f'{eid}.json').read_text())['decision'] == 'allow'
    assert flag.exists()
    payload = json.loads(flag.read_text())
    assert payload['agent_source'] == 'claude' and 'created_at' in payload
    # 标志生效中：新条目不上岛，直接 auto-allow
    eid2 = _enqueue(bridge)
    deadline = time.time() + 4
    auto = bridge['resp_dir'] / f'{eid2}.json'
    while time.time() < deadline and not auto.exists():
        time.sleep(0.2)
    assert auto.exists() and json.loads(auto.read_text())['decision'] == 'allow'
    _c, state = _api('/api/state')
    assert eid2 not in [p['id'] for p in state['pending']]
    flag.unlink()
    for f in (bridge['resp_dir'] / f'{eid}.json', auto):
        f.unlink(missing_ok=True)


# ── codex 条目走 codex 标志 ──────────────────────────────────────────
def test_codex_always_flag(bridge):
    eid = _enqueue(bridge, agent_source='codex')
    _wait_pending(eid)
    _api('/api/decision', {'id': eid, 'decision': 'always'})
    assert (bridge['tmp'] / 'always_codex').exists()
    assert not (bridge['tmp'] / 'always_claude').exists()
    (bridge['tmp'] / 'always_codex').unlink()
    (bridge['resp_dir'] / f'{eid}.json').unlink(missing_ok=True)


# ── notify 类型不进 pending、不写响应 ────────────────────────────────
def test_notify_entry(bridge):
    eid = _enqueue(bridge, id=f'notify_{time.time_ns()}', type='notify',
                   hook_event_name='stop')
    deadline = time.time() + 4
    while time.time() < deadline:
        _c, state = _api('/api/state')
        if eid in [n['id'] for n in state['notify']]:
            break
        time.sleep(0.2)
    else:
        pytest.fail('notify 未出现')
    assert eid not in [p['id'] for p in state['pending']]
    assert not (bridge['resp_dir'] / f'{eid}.json').exists()


# ── T9: 队列截断后不崩、新条目仍可达 ─────────────────────────────────
def test_queue_truncation(bridge):
    bridge['queue'].write_text('')          # 模拟 monitor.py 截断
    time.sleep(1.0)
    eid = _enqueue(bridge)
    _wait_pending(eid)
    _api('/api/decision', {'id': eid, 'decision': 'allow'})
    (bridge['resp_dir'] / f'{eid}.json').unlink(missing_ok=True)


# ── T10/T11: 未知/过期条目决策返回 410，不写文件 ─────────────────────
def test_unknown_decision(bridge):
    code, body = _api('/api/decision', {'id': 'ghost_123', 'decision': 'allow'})
    assert code == 410
    assert not (bridge['resp_dir'] / 'ghost_123.json').exists()


# ── T12: 连续 5 条排队不丢、有序 ─────────────────────────────────────
def test_queue_burst(bridge):
    ids = [_enqueue(bridge) for _ in range(5)]
    state = _wait_pending(ids[-1])
    pend_ids = [p['id'] for p in state['pending']]
    assert all(i in pend_ids for i in ids)
    # 到达顺序保持
    pos = [pend_ids.index(i) for i in ids]
    assert pos == sorted(pos)
    for i in ids:
        _api('/api/decision', {'id': i, 'decision': 'deny'})
        (bridge['resp_dir'] / f'{i}.json').unlink(missing_ok=True)


# ── 防重启风暴：启动时跳过历史条目（独立进程验证） ────────────────────
def test_skip_history_on_start(bridge):
    tmp = tempfile.mkdtemp(prefix='island_hist_')
    queue = Path(tmp) / 'queue.jsonl'
    queue.write_text(json.dumps({'id': 'hist_1', 'tool_name': 'Bash'}) + '\n')
    env = dict(os.environ, ISLAND_QUEUE_FILE=str(queue),
               ISLAND_RESP_DIR=str(Path(tmp) / 'r'))
    proc = subprocess.Popen([sys.executable, str(BRIDGE), '--port', '5597'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                with urllib.request.urlopen('http://127.0.0.1:5597/api/state', timeout=2) as r:
                    state = json.loads(r.read())
                break
            except Exception:
                time.sleep(0.2)
        time.sleep(1.5)
        with urllib.request.urlopen('http://127.0.0.1:5597/api/state', timeout=2) as r:
            state = json.loads(r.read())
        assert 'hist_1' not in [p['id'] for p in state['pending']]
    finally:
        proc.kill()
        proc.wait()


# ── 超时自动放行：普通审批到点放行，ask/plan 永不自动批 ────────────────
def test_auto_allow_timeout(bridge):
    code, body = _api('/api/settings', {'auto_allow_timeout': 1})
    assert code == 200 and body['settings']['auto_allow_timeout'] == 1
    try:
        eid = _enqueue(bridge)
        _wait_pending(eid)
        deadline = time.time() + 6
        resp = bridge['resp_dir'] / f'{eid}.json'
        while time.time() < deadline and not resp.exists():
            _api('/api/state')          # 保持 client 活跃（倒计时展示前提）
            time.sleep(0.3)
        assert resp.exists(), '超时未自动放行'
        assert json.loads(resp.read_text())['decision'] == 'allow'
        resp.unlink()

        # ask 类型不受超时放行影响
        ask_id = _enqueue(bridge, tool_name='AskUserQuestion',
                          tool_input={'questions': [{'question': 'q?', 'options':
                                      [{'label': 'a'}, {'label': 'b'}]}]})
        _wait_pending(ask_id)
        time.sleep(2.5)
        _c, state = _api('/api/state')
        assert ask_id in [p['id'] for p in state['pending']], 'ask 不应被自动放行'
        assert not (bridge['resp_dir'] / f'{ask_id}.json').exists()
        _api('/api/decision', {'id': ask_id, 'decision': 'deny'})
        (bridge['resp_dir'] / f'{ask_id}.json').unlink(missing_ok=True)
    finally:
        _api('/api/settings', {'auto_allow_timeout': 0})


# ── 会话级 YOLO：开后该会话秒放行，ask 仍上岛；关后恢复上岛 ──────────
def test_session_yolo(bridge):
    code, body = _api('/api/session_yolo', {'session_id': 'yolo-s', 'on': True})
    assert code == 200 and 'yolo-s' in body['yolo_sessions']
    eid = _enqueue(bridge, session_id='yolo-s')
    deadline = time.time() + 4
    resp = bridge['resp_dir'] / f'{eid}.json'
    while time.time() < deadline and not resp.exists():
        time.sleep(0.2)
    assert resp.exists() and json.loads(resp.read_text())['decision'] == 'allow'
    resp.unlink()
    _c, state = _api('/api/state')
    assert eid not in [p['id'] for p in state['pending']]

    # ask 不受 YOLO 影响
    ask_id = _enqueue(bridge, session_id='yolo-s', tool_name='AskUserQuestion',
                      tool_input={'questions': [{'question': 'q?', 'options':
                                  [{'label': 'a'}, {'label': 'b'}]}]})
    _wait_pending(ask_id)
    _api('/api/decision', {'id': ask_id, 'decision': 'deny'})
    (bridge['resp_dir'] / f'{ask_id}.json').unlink(missing_ok=True)

    # 关闭后恢复正常上岛
    _api('/api/session_yolo', {'session_id': 'yolo-s', 'on': False})
    eid2 = _enqueue(bridge, session_id='yolo-s')
    _wait_pending(eid2)
    _api('/api/decision', {'id': eid2, 'decision': 'deny'})
    (bridge['resp_dir'] / f'{eid2}.json').unlink(missing_ok=True)


# ── SSH 远程聚合：副桥(模拟远程) → 主桥合并视图 + 决策转发 ────────────
def test_remote_aggregation(bridge):
    import subprocess as sp
    import tempfile as tf
    rport = 5590   # 勿用 PORT+1=5599：生产桥端口，曾撞车误连
    rtmp = Path(tf.mkdtemp(prefix='island_remote_'))
    (rtmp / 'responses').mkdir()
    renv = dict(os.environ,
                ISLAND_QUEUE_FILE=str(rtmp / 'queue.jsonl'),
                ISLAND_RESP_DIR=str(rtmp / 'responses'),
                ISLAND_STATE_DIR=str(rtmp),
                ISLAND_SETTINGS_FILE=str(rtmp / 'settings.json'),
                ISLAND_RL_CACHE=str(rtmp / 'rl.json'))
    rproc = sp.Popen([sys.executable, str(BRIDGE), '--port', str(rport), '--debug'],
                     env=renv, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    try:
        for _ in range(50):
            try:
                if _api_port(rport, '/api/health')[0] == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.fail('远程沙箱桥未能启动')

        # 主桥挂载远程
        _api('/api/settings', {'remotes': [
            {'name': 'r1', 'url': f'http://127.0.0.1:{rport}', 'ssh': 'ssh test'}]})

        # 远程入队 → 主桥合并视图可见（带 _remote 标）
        entry = {'id': f'rmt_{time.time_ns()}', 'session_id': 'rs-1',
                 'tool_name': 'Bash', 'tool_input': {'command': 'echo remote'}}
        with open(rtmp / 'queue.jsonl', 'a') as f:
            f.write(json.dumps(entry) + '\n')
        # 链路 = 远程桥 tailer 采集 + 主桥 remote_poller(3~5s 周期)两级轮询，
        # 10s 死线负载下会输（曾致套件 flaky）→ 20s
        deadline = time.time() + 20
        found = None
        while time.time() < deadline:
            _c, state = _api('/api/state')
            found = next((p for p in state['pending'] if p['id'] == entry['id']), None)
            if found:
                break
            time.sleep(0.4)
        assert found, '远程 pending 未出现在主桥合并视图'
        assert found['_remote'] == 'r1'

        # 主桥决策 → 转发 → 远程响应文件落地
        code, body = _api('/api/decision', {'id': entry['id'], 'decision': 'allow'})
        assert code == 200 and body['ok'] and body.get('remote') == 'r1'
        resp = rtmp / 'responses' / f"{entry['id']}.json"
        deadline = time.time() + 4
        while time.time() < deadline and not resp.exists():
            time.sleep(0.2)
        assert resp.exists()
        assert json.loads(resp.read_text())['decision'] == 'allow'
    finally:
        _api('/api/settings', {'remotes': []})
        rproc.kill()
        rproc.wait()


def _api_port(port, path, payload=None):
    url = f'http://127.0.0.1:{port}{path}'
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if data else 'GET')
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


# ── 响应文件原子写：读端在文件出现瞬间解析必须永远成功 ─────────────────
# 背景（2026-07-03）：write_response 曾用 write_text（open 与 write 之间存在
# 空文件窗口），hook 轮询撞进窗口 → 解析失败 → 兜底 allow（用户 deny 被反转）。
# 本测试直连 write_response 压测：修复前 3000 次 ~45% 失败，原子写后必须为 0。
def test_response_write_atomic(tmp_path):
    import importlib.util
    import threading
    os.environ['ISLAND_STATE_DIR'] = str(tmp_path)
    os.environ['ISLAND_RESP_DIR'] = str(tmp_path / 'responses')
    os.environ['ISLAND_SETTINGS_FILE'] = str(tmp_path / 'settings.json')
    spec = importlib.util.spec_from_file_location('ib_atomic_test', str(BRIDGE))
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)

    n, failures = 2000, []

    def writer():
        for i in range(n):
            ib.write_response(f'atom_{i}', 'deny', 'race-test')

    t = threading.Thread(target=writer)
    t.start()
    for i in range(n):
        p = tmp_path / 'responses' / f'atom_{i}.json'
        deadline = time.time() + 5
        while not p.exists():
            assert time.time() < deadline, f'等待 {i} 超时'
        try:
            assert json.loads(p.read_text())['decision'] == 'deny'
        except (json.JSONDecodeError, KeyError) as e:
            failures.append((i, type(e).__name__))
        p.unlink()
    t.join()
    assert not failures, f'读端撞到非原子写窗口 {len(failures)}/{n} 次: {failures[:3]}'
