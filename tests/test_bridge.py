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


def _sandbox_env(tmp, **extra):
    """沙箱桥的 env：状态目录、桥日志、Kimi 凭证全部落在 tmp。
    （2026-09-26：此前只隔离了队列/响应，夜检每晚往生产桥日志写一批假审批，
    统计审批数据时会被当成真实流量；状态目录也没隔离，Kimi 额度 poller
    会拿真实凭证去查、写真实缓存）"""
    tmp = Path(tmp)
    return dict(os.environ,
                ISLAND_STATE_DIR=str(tmp),
                ISLAND_BRIDGE_LOG=str(tmp / 'bridge.log'),
                ISLAND_KIMI_CRED=str(tmp / 'no-kimi-cred.json'),
                **extra)


@pytest.fixture(scope='module')
def bridge():
    """启动隔离沙箱 bridge 子进程。"""
    tmp = tempfile.mkdtemp(prefix='island_test_')
    queue = Path(tmp) / 'queue.jsonl'
    resp_dir = Path(tmp) / 'responses'
    env = _sandbox_env(tmp,
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


# ── 入队到上岛的延迟（2026-09-26：队列尾随 0.5s → 0.1s，要人审的调用少等约 0.4s） ──
def test_pending_visible_quickly(bridge):
    lat = []
    for _ in range(5):
        eid = _enqueue(bridge)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3:
            _c, st = _api('/api/state')
            if eid in [p['id'] for p in st['pending']]:
                break
            time.sleep(0.01)
        lat.append(time.monotonic() - t0)
        _api('/api/decision', {'id': eid, 'decision': 'allow'})
        (bridge['resp_dir'] / f'{eid}.json').unlink(missing_ok=True)
    lat.sort()
    print(f'入队到上岛延迟 {[round(x, 2) for x in lat]}')
    assert lat[2] < 0.15, f'入队到上岛中位 {lat[2]:.2f}s（各次 {[round(x, 2) for x in lat]}）'


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
    env = _sandbox_env(tmp, ISLAND_QUEUE_FILE=str(queue),
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
    renv = _sandbox_env(rtmp,
                ISLAND_QUEUE_FILE=str(rtmp / 'queue.jsonl'),
                ISLAND_RESP_DIR=str(rtmp / 'responses'),
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


# ── 启动回放在途审批：桥重启不该孤儿掉正在等审批的工具调用 ─────────────
# 背景（2026-07-04）：queue_tailer 启动"跳到文件末尾"，桥重启瞬间入队、hook 正
# 阻塞等审批的条目被永久漏读 → hook 干等超时 → 回落原生提示（文件写这类不在
# 白名单的工具首当其冲，用户被硬生生卡住）。修复：启动回放最近仍在途的审批。
def test_replay_inflight_on_restart(tmp_path):
    import importlib.util
    os.environ['ISLAND_STATE_DIR'] = str(tmp_path)
    os.environ['ISLAND_QUEUE_FILE'] = str(tmp_path / 'queue.jsonl')
    os.environ['ISLAND_RESP_DIR'] = str(tmp_path / 'responses')
    os.environ['ISLAND_SETTINGS_FILE'] = str(tmp_path / 'settings.json')
    spec = importlib.util.spec_from_file_location('ib_replay', str(BRIDGE))
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)

    now = 1_000_000.0
    fresh = {'id': f'aaa111_{int(now - 5)}', 'tool_name': 'Write', 'session_id': 's1',
             'tool_input': {'file_path': '/x/mem.md', 'content': 'x'}}        # 在途，应回放
    old = {'id': f'bbb222_{int(now - 500)}', 'tool_name': 'Write', 'session_id': 's1',
           'tool_input': {'file_path': '/x/y.md'}}                            # 过旧，不回放
    answered = {'id': f'ccc333_{int(now - 5)}', 'tool_name': 'Bash', 'session_id': 's1',
                'tool_input': {'command': 'ls'}}                             # 已有响应文件，不回放
    (tmp_path / 'responses').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'responses' / f'{answered["id"]}.json').write_text('{"decision":"allow"}')
    with open(tmp_path / 'queue.jsonl', 'w') as f:
        for e in (old, answered, fresh):
            f.write(json.dumps(e) + '\n')

    off = ib.replay_inflight_queue(now)
    pending = set(ib.STATE.pending.keys())
    assert fresh['id'] in pending, '在途 Write 审批应被回放上岛'
    assert old['id'] not in pending, '超窗口的过旧条目不应回放'
    assert answered['id'] not in pending, '已有响应文件的条目不应回放（hook 会自取）'
    assert off == (tmp_path / 'queue.jsonl').stat().st_size, '返回的 offset 应为文件末尾'


# ── always 标志豁免 ask/plan：选择题/计划永远上岛，绝不被自动放行 ─────────
# 背景（2026-07-05）：always_claude 生效时，AskUserQuestion 被 always 秒"放行"→
# 掉回终端原生选择题，绕过岛上作答。always 检查须与 yolo/超时一样豁免 ask/plan。
def test_always_flag_exempts_ask_plan(tmp_path):
    import importlib.util
    os.environ['ISLAND_STATE_DIR'] = str(tmp_path)
    os.environ['ISLAND_QUEUE_FILE'] = str(tmp_path / 'queue.jsonl')
    os.environ['ISLAND_RESP_DIR'] = str(tmp_path / 'responses')
    os.environ['ISLAND_SETTINGS_FILE'] = str(tmp_path / 'settings.json')
    os.environ['ISLAND_ALWAYS_CLAUDE'] = str(tmp_path / 'always_claude')
    spec = importlib.util.spec_from_file_location('ib_always', str(BRIDGE))
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)

    # always 标志生效中
    (tmp_path / 'always_claude').write_text('{"agent_source":"claude"}')

    ask = {'id': 'q1_1', 'tool_name': 'AskUserQuestion', 'session_id': 's1', 'tool_input': {}}
    plan = {'id': 'p1_2', 'tool_name': 'ExitPlanMode', 'session_id': 's1', 'tool_input': {}}
    bash = {'id': 'b1_3', 'tool_name': 'Bash', 'session_id': 's1', 'tool_input': {'command': 'ls'}}
    for e in (ask, plan, bash):
        ib.STATE.add_entry(e)

    pending = ib.STATE.pending
    assert 'q1_1' in pending and pending['q1_1'].get('kind') == 'ask', 'always 生效时选择题仍须上岛'
    assert 'p1_2' in pending and pending['p1_2'].get('kind') == 'plan', 'always 生效时计划审阅仍须上岛'
    assert 'b1_3' not in pending, '普通工具在 always 下应被自动放行（不上岛）'
    assert (tmp_path / 'responses' / 'b1_3.json').exists(), 'Bash 应写了 allow 响应'


def _fresh_bridge_module(tmp_path, name):
    """在隔离状态目录里载入一份全新的桥模块（等同一次重启）。"""
    import importlib.util
    os.environ['ISLAND_STATE_DIR'] = str(tmp_path)
    os.environ['ISLAND_QUEUE_FILE'] = str(tmp_path / 'queue.jsonl')
    os.environ['ISLAND_RESP_DIR'] = str(tmp_path / 'responses')
    os.environ['ISLAND_SETTINGS_FILE'] = str(tmp_path / 'settings.json')
    os.environ['ISLAND_ALWAYS_CLAUDE'] = str(tmp_path / 'always_claude')
    spec = importlib.util.spec_from_file_location(name, str(BRIDGE))
    ib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ib)
    return ib


# ── Always 只对点它的那个会话生效（2026-09-26） ──────────────────────────
# 旧行为：always_claude 是按 CLI 全局的标志——A 会话点 Always，B 会话的命令也被
# 直接放行；任何会话答完一轮又把它清掉。并发多会话时既可能放过不该放的，又显得时灵时不灵。
def test_always_scoped_to_session(tmp_path):
    ib = _fresh_bridge_module(tmp_path, 'ib_always_scope')
    (tmp_path / 'always_claude').write_text(
        json.dumps({'agent_source': 'claude', 'session_id': 'sA'}))
    ib.STATE.add_entry({'id': 'a_1', 'tool_name': 'Bash', 'session_id': 'sA',
                        'tool_input': {'command': 'ls'}})
    ib.STATE.add_entry({'id': 'b_2', 'tool_name': 'Bash', 'session_id': 'sB',
                        'tool_input': {'command': 'rm -rf build'}})
    assert 'a_1' not in ib.STATE.pending, '点了 Always 的会话自己应自动放行'
    assert 'b_2' in ib.STATE.pending, '别的会话不得被 A 的 Always 放行'
    assert not (tmp_path / 'responses' / 'b_2.json').exists()


def test_always_legacy_flag_without_session_stays_global(tmp_path):
    """旧格式标志（无 session_id）维持原全局语义，升级过渡期不误拦。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_always_legacy')
    (tmp_path / 'always_claude').write_text('{"agent_source":"claude"}')
    ib.STATE.add_entry({'id': 'c_3', 'tool_name': 'Bash', 'session_id': 'sC',
                        'tool_input': {'command': 'ls'}})
    assert 'c_3' not in ib.STATE.pending


# ── YOLO 名单落盘：桥重启不丢，钩子可直接读（2026-09-26） ────────────────
def test_yolo_persisted_across_restart(tmp_path):
    ib = _fresh_bridge_module(tmp_path, 'ib_yolo_1')
    ib.STATE.set_yolo('s-keep', True)
    ib.STATE.set_yolo('s-drop', True)
    ib.STATE.set_yolo('s-drop', False)
    f = tmp_path / 'yolo_sessions.json'
    assert json.loads(f.read_text()) == ['s-keep'], '名单须以 JSON 数组落盘（钩子读它）'
    ib2 = _fresh_bridge_module(tmp_path, 'ib_yolo_2')          # 模拟重启
    assert ib2.STATE.yolo_sessions == {'s-keep'}
    ib2.STATE.add_entry({'id': 'y_4', 'tool_name': 'Bash', 'session_id': 's-keep',
                         'tool_input': {'command': 'ls'}})
    assert 'y_4' not in ib2.STATE.pending, '重启后 YOLO 会话仍应秒放行'


# ── Claude 会话上下文占用：读 statusline 包装按会话写的 ctx/<sid>.json ─────────
def test_claude_context_pct_from_statusline_cache(tmp_path):
    ib = _fresh_bridge_module(tmp_path, 'ib_ctx')
    (tmp_path / 'ctx').mkdir()
    (tmp_path / 'ctx' / 'sid-a.json').write_text('{"pct": 88, "ts": 1}')
    tr = tmp_path / 'sid-a.jsonl'
    tr.write_text('{"type":"user"}\n')
    a = {'session_id': 'sid-a', 'file': str(tr)}
    b = {'session_id': 'sid-b', 'file': str(tr)}
    ib._claude_session_extras(a)
    ib._claude_session_extras(b)
    assert a.get('context_pct') == 88
    assert 'context_pct' not in b, '没有缓存就不显示，别给假数'


def test_ctx_cache_pruned_after_a_week(tmp_path):
    ib = _fresh_bridge_module(tmp_path, 'ib_ctx_prune')
    (tmp_path / 'ctx').mkdir()
    old, new = tmp_path / 'ctx' / 'old.json', tmp_path / 'ctx' / 'new.json'
    old.write_text('{"pct": 1}'); new.write_text('{"pct": 2}')
    t = time.time() - 8 * 86400
    os.utime(old, (t, t))
    ib.cleanup_ctx_cache()
    assert not old.exists() and new.exists()


# ── PermissionRequest 卡（2026-09-26） ─────────────────────────
def _pr_entry(eid='pr_abc_1', sid='sP', agent=None):
    e = {'id': eid, 'hook_event_name': 'PermissionRequest', 'session_id': sid, 'tool_name': 'Bash',
         'tool_input': {'command': 'for id in a b; do rm -f $id/x; done'}, 'permission_suggestions': [],
         'island_perm': 1}
    if agent:
        e['agent_source'] = agent
    return e


def test_perm_card_never_auto_allowed(tmp_path):
    """本会话 Always、YOLO、超时自动放行都不能放走 perm 卡。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_1')
    (tmp_path / 'always_claude').write_text(json.dumps({'agent_source': 'claude', 'session_id': 'sP'}))
    ib.STATE.set_yolo('sP', True)
    ib.STATE.add_entry(_pr_entry())
    assert ib.STATE.pending.get('pr_abc_1', {}).get('kind') == 'perm'
    assert not (tmp_path / 'responses' / 'pr_abc_1.json').exists()
    ib.STATE.update_settings({'auto_allow_timeout': 1})
    ib.STATE.last_client = time.time()
    ib.STATE.pending['pr_abc_1']['_arrived'] -= 30
    ib.STATE.expire()
    assert 'pr_abc_1' in ib.STATE.pending, '超时自动放行不得作用于 perm 卡'
    assert not (tmp_path / 'responses' / 'pr_abc_1.json').exists()


def test_perm_cancel_removes_card(tmp_path):
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_2')
    ib.STATE.add_entry(_pr_entry())
    ib.STATE.add_entry({'type': 'cancel', 'id': 'pr_abc_1'})
    assert 'pr_abc_1' not in ib.STATE.pending, '终端先答/钩子超时的撤卡标记须撤掉卡片'


def test_perm_ttl_covers_hook_wait(tmp_path):
    """钩子最长等 110s：普通卡 40s 就过期，perm 卡要撑到 115s。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_3')
    ib.STATE.add_entry(_pr_entry())
    ib.STATE.pending['pr_abc_1']['_arrived'] -= 60
    ib.STATE.expire()
    assert 'pr_abc_1' in ib.STATE.pending
    ib.STATE.pending['pr_abc_1']['_arrived'] -= 60
    ib.STATE.expire()
    assert 'pr_abc_1' not in ib.STATE.pending


def test_codex_permission_request_unchanged(tmp_path):
    """Codex 自家 PermissionRequest 钩子的条目（不带 island_perm 标记）维持原语义，不归入 perm。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_4')
    e = _pr_entry('codexpr_x_1', agent='codex')
    e.pop('island_perm')
    ib.STATE.add_entry(e)
    assert ib.STATE.pending['codexpr_x_1'].get('kind') != 'perm'


def test_perm_tag_wins_over_agent_source(tmp_path):
    """审查#3：本钩子条目即使来源被误配成 codex，也按 perm 处理、永不自动放行。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_5')
    (tmp_path / 'always_codex').write_text(json.dumps({'agent_source': 'codex', 'session_id': 'sP'}))
    ib.STATE.add_entry(_pr_entry('pr_cx-1_1', agent='codex'))
    assert ib.STATE.pending.get('pr_cx-1_1', {}).get('kind') == 'perm'


def test_perm_does_not_override_ask_plan(tmp_path):
    """审查#1：带 perm 标记的选择题/计划条目仍按 ask/plan 走岛上作答（纵深防御）。"""
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_6')
    for i, tool in enumerate(('AskUserQuestion', 'ExitPlanMode')):
        e = _pr_entry(f'pr_ap-{i}_1')
        e['tool_name'] = tool
        ib.STATE.add_entry(e)
    assert ib.STATE.pending['pr_ap-0_1']['kind'] == 'ask'
    assert ib.STATE.pending['pr_ap-1_1']['kind'] == 'plan'


def test_perm_replay_on_restart(tmp_path):
    """桥重启回放：已撤的 perm 卡不得复活；90 秒前入队、仍在等的 perm 卡要回放（钩子最长等 110s）。"""
    now = time.time()
    q = tmp_path / 'queue.jsonl'
    waiting = _pr_entry(f'pr_wait_{int(now - 90)}')
    gone = _pr_entry(f'pr_gone_{int(now - 5)}')
    q.write_text('\n'.join(json.dumps(e) for e in
                           (waiting, gone, {'type': 'cancel', 'id': gone['id']})) + '\n')
    ib = _fresh_bridge_module(tmp_path, 'ib_perm_replay')
    ib.replay_inflight_queue(now)
    assert waiting['id'] in ib.STATE.pending, '仍在等的 perm 卡须回放'
    assert gone['id'] not in ib.STATE.pending, '已撤的卡不得复活'


def test_perm_always_decision_is_plain_allow(bridge):
    """岛上对 perm 卡按 Always（含热键 Ctrl+Alt+S）只算一次允许，不写 Always 标志。"""
    flag = bridge['tmp'] / 'always_claude'
    flag.unlink(missing_ok=True)
    eid = _enqueue(bridge, **_pr_entry(f'pr_http_{time.time_ns()}'))
    _wait_pending(eid)
    code, _b = _api('/api/decision', {'id': eid, 'decision': 'always'})
    assert code == 200
    assert json.loads((bridge['resp_dir'] / f'{eid}.json').read_text())['decision'] == 'allow'
    assert not flag.exists(), 'perm 卡不得写 Always 标志'
    (bridge['resp_dir'] / f'{eid}.json').unlink(missing_ok=True)


# ── /api/show 展示请求（弹窗看图/看 demo 中继） ──────────────────────
def test_show_enqueue_and_state(bridge):
    """正常路径：入队后 /api/state 的 show 列表可见、字段齐全、seq 递增。"""
    f = bridge['tmp'] / 'demo.png'
    f.write_bytes(b'fake-png')
    code, body = _api('/api/show', {'kind': 'image', 'path': str(f),
                                    'win_path': 'D:\\fake\\demo.png'})
    assert code == 200 and body['ok']
    seq1 = body['seq']
    _c, state = _api('/api/state')
    items = state.get('show') or []
    assert any(e['seq'] == seq1 and e['kind'] == 'image'
               and e['win_path'] == 'D:\\fake\\demo.png'
               and e['name'] == 'demo.png' for e in items), items
    code, body2 = _api('/api/show', {'kind': 'html', 'path': str(f),
                                     'win_path': 'D:\\fake\\demo.png', 'raw': True})
    assert code == 200 and body2['seq'] > seq1, 'seq 必须单调递增'
    _c, state2 = _api('/api/state')
    ent2 = next(e for e in state2['show'] if e['seq'] == body2['seq'])
    assert ent2['raw'] is True, 'raw 直通标志必须随条目往返'
    ent1 = next(e for e in state2['show'] if e['seq'] == seq1)
    assert ent1['raw'] is False, '未传 raw 默认 False'
    for k in ('pdf', 'md'):
        code, b = _api('/api/show', {'kind': k, 'path': str(f),
                                     'win_path': 'D:\\fake\\x'})
        assert code == 200 and b['ok'], f'kind={k} 应被放行'


def test_show_rejects_bad_input(bridge):
    """异常路径：坏 kind=400、缺 win_path=400、WSL 路径不存在=404（不入队）。"""
    f = bridge['tmp'] / 'x.html'
    f.write_text('<b>x</b>')
    _c, state0 = _api('/api/state')
    n0 = len(state0.get('show') or [])
    code, _ = _api('/api/show', {'kind': 'exe', 'path': str(f), 'win_path': 'D:\\x'})
    assert code == 400
    code, _ = _api('/api/show', {'kind': 'html', 'path': str(f), 'win_path': ''})
    assert code == 400
    code, _ = _api('/api/show', {'kind': 'html', 'path': str(f) + '.nope',
                                 'win_path': 'D:\\x'})
    assert code == 404
    _c, state1 = _api('/api/state')
    assert len(state1.get('show') or []) == n0, '被拒请求不得入队'


# ── 条目自带 ttl（2026-09-04，外部助手的决策卡上岛）─────────────────────
def test_entry_ttl_overrides_default_expiry(bridge):
    """kind=ask 默认 125s 过期；带 ttl 的条目按自己的 ttl 活着，不带的照旧。"""
    short = _enqueue(bridge, id=f'ttl_short_{time.time_ns()}', kind='ask', tool_name='helper',
                     ttl=1, tool_input={'questions': [{'question': 'q', 'options': [{'label': 'A'}]}]})
    long_ = _enqueue(bridge, id=f'ttl_long_{time.time_ns()}', kind='ask', tool_name='helper',
                     ttl=3600, tool_input={'questions': [{'question': 'q', 'options': [{'label': 'A'}]}]})
    _wait_pending(long_)
    _wait_pending(short, present=False, timeout=6)     # 1s ttl → 很快过期
    _c, state = _api('/api/state')
    assert long_ in [p['id'] for p in state['pending']], '长 ttl 的还在'
    # 自带 kind=ask 的条目不会被 Always 标志自动放行（与 AskUserQuestion 同待遇）
    assert not (bridge['resp_dir'] / f'{long_}.json').exists()
