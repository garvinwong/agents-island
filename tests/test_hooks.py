#!/usr/bin/env python3
"""Claude 审批钩子 pre_tool_use.sh / notify_hook.sh 行为测试（沙箱状态目录，不碰真实队列）。

复现并锁死的问题（2026-09-26 实测）：
  ① 已开 ⚡YOLO / 已点 Always 的调用也要排队等桥，钩子每 1 秒才看一次答复——
     最近 12 个会话 4003 次放行耗时中位 1.13s、p90 1.30s，累计约 95 分钟纯等待；
  ② 真正要人审的调用，点完允许后平均还要再等 0.5s（同一个 1 秒轮询）；
  ③ 任何会话答完一轮（甚至只是 Notification 提醒）都会清掉 Always，
     与“Always 只对点它的会话生效”配套须改为只清本会话的。

运行：cd apps/agents-island && python3 -m pytest tests/test_hooks.py -v
"""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
PRE = HOOKS / 'pre_tool_use.sh'
NOTIFY = HOOKS / 'notify_hook.sh'
ALLOW = {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow'}}


@pytest.fixture
def sb(tmp_path):
    env = dict(os.environ,
               ISLAND_STATE_DIR=str(tmp_path),
               ISLAND_QUEUE_FILE=str(tmp_path / 'queue.jsonl'),
               ISLAND_RESP_DIR=str(tmp_path / 'responses'),
               ISLAND_ALWAYS_CLAUDE=str(tmp_path / 'always_claude'),
               ISLAND_HOOK_TIMEOUT='2',
               ISLAND_HOOK_TIMEOUT_ASK='2')
    env.pop('ISLAND_AGENT_SOURCE', None)
    return {'dir': tmp_path, 'env': env}


def run_hook(sb, payload, script=PRE):
    t0 = time.monotonic()
    p = subprocess.run(['bash', str(script)], input=json.dumps(payload), env=sb['env'],
                       capture_output=True, text=True, timeout=30)
    return p, time.monotonic() - t0


def queued_ids(sb):
    q = sb['dir'] / 'queue.jsonl'
    if not q.exists():
        return []
    return [json.loads(l)['id'] for l in q.read_text().splitlines() if l.strip()]


def bash_call(sid, cmd='ls'):
    return {'session_id': sid, 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash',
            'tool_input': {'command': cmd}}


# ── ① 快速通道 ──────────────────────────────────────────────────────────
def test_yolo_session_allowed_without_queue(sb):
    (sb['dir'] / 'yolo_sessions.json').write_text(json.dumps(['sY']))
    p, dt = run_hook(sb, bash_call('sY'))
    assert json.loads(p.stdout) == ALLOW
    assert dt < 0.5, f'YOLO 放行耗时 {dt:.2f}s（旧版 ≥1s）'
    assert queued_ids(sb) == [], 'YOLO 调用不应再排队等桥'


def test_always_same_session_fast_other_session_queued(sb):
    (sb['dir'] / 'always_claude').write_text(
        json.dumps({'agent_source': 'claude', 'session_id': 'sA'}))
    p, dt = run_hook(sb, bash_call('sA'))
    assert json.loads(p.stdout) == ALLOW and dt < 0.5
    assert queued_ids(sb) == []
    p, _ = run_hook(sb, bash_call('sB', 'rm -rf build'))
    assert len(queued_ids(sb)) == 1, '别的会话不得走 A 的 Always 快速通道'
    assert json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'] == 'defer'


def test_ask_and_plan_never_fast(sb):
    """选择题/计划即使在 YOLO 会话里也必须上岛（与桥侧豁免一致）。"""
    (sb['dir'] / 'yolo_sessions.json').write_text(json.dumps(['sY']))
    for tool in ('AskUserQuestion', 'ExitPlanMode'):
        run_hook(sb, {'session_id': 'sY', 'tool_name': tool, 'tool_input': {}})
    assert len(queued_ids(sb)) == 2


def test_broken_yolo_file_falls_back_to_queue(sb):
    (sb['dir'] / 'yolo_sessions.json').write_text('{not json')
    run_hook(sb, bash_call('sY'))
    assert len(queued_ids(sb)) == 1


# ── ② 慢路径：答复到达后立即返回 ──────────────────────────────────────
def _responder(sb, decision, reason='', delay=0.0):
    """模拟桥：看到队列新条目就（延时后）原子写答复文件。"""
    stop = threading.Event()

    def loop():
        seen = set()
        rd = sb['dir'] / 'responses'
        while not stop.is_set():
            for eid in queued_ids(sb):
                if eid in seen:
                    continue
                seen.add(eid)
                time.sleep(delay)
                rd.mkdir(exist_ok=True)
                tmp = rd / f'.{eid}.tmp'
                body = {'decision': decision}
                if reason:
                    body['reason'] = reason
                tmp.write_text(json.dumps(body, ensure_ascii=False))
                tmp.replace(rd / f'{eid}.json')
            time.sleep(0.01)

    th = threading.Thread(target=loop, daemon=True)
    th.start()
    return stop


def test_slow_path_returns_promptly_after_decision(sb):
    stop = _responder(sb, 'allow', delay=0.3)
    try:
        p, dt = run_hook(sb, bash_call('sN'))
    finally:
        stop.set()
    assert json.loads(p.stdout) == ALLOW
    assert dt < 0.75, f'答复 0.3s 就绪后钩子 {dt:.2f}s 才返回（旧版约 1.1s）'
    assert not list((sb['dir'] / 'responses').glob('*.json')), '钩子读后须删掉答复文件'


def test_slow_path_deny_keeps_reason(sb):
    stop = _responder(sb, 'deny', reason='[用户已在 Agents Island 面板作答] 选择「蓝绿」')
    try:
        p, _ = run_hook(sb, bash_call('sN'))
    finally:
        stop.set()
    out = json.loads(p.stdout)['hookSpecificOutput']
    assert out['permissionDecision'] == 'deny'
    assert out['permissionDecisionReason'] == '[用户已在 Agents Island 面板作答] 选择「蓝绿」'


def test_timeout_defers(sb):
    p, dt = run_hook(sb, bash_call('sN'))
    assert json.loads(p.stdout)['hookSpecificOutput']['permissionDecision'] == 'defer'
    assert 1.5 < dt < 4


# ── ③ notify_hook：只清本会话、只在一轮结束时清 ───────────────────────
def _flag(sb, sid):
    f = sb['dir'] / 'always_claude'
    f.write_text(json.dumps({'agent_source': 'claude', 'session_id': sid}))
    return f


def test_stop_of_other_session_keeps_always(sb):
    f = _flag(sb, 'sA')
    run_hook(sb, {'session_id': 'sB', 'hook_event_name': 'Stop'}, NOTIFY)
    assert f.exists(), 'B 答完一轮不应清掉 A 的 Always'


def test_notification_does_not_clear_always(sb):
    f = _flag(sb, 'sA')
    run_hook(sb, {'session_id': 'sA', 'hook_event_name': 'Notification',
                  'notification_type': 'idle_prompt'}, NOTIFY)
    assert f.exists(), 'Notification 提醒不是一轮结束'


def test_stop_of_same_session_clears_always(sb):
    f = _flag(sb, 'sA')
    run_hook(sb, {'session_id': 'sA', 'hook_event_name': 'Stop'}, NOTIFY)
    assert not f.exists()
    assert queued_ids(sb), 'Stop 通知仍须入队'


def test_stop_clears_legacy_flag_without_session(sb):
    f = sb['dir'] / 'always_claude'
    f.write_text('{"agent_source":"claude"}')
    run_hook(sb, {'session_id': 'sZ', 'hook_event_name': 'Stop'}, NOTIFY)
    assert not f.exists(), '旧格式全局标志维持原语义：任一会话一轮结束即清'


# ── 真链路：沙箱桥 + 钩子（桥写的 YOLO 文件钩子能读；桥代答后钩子及时返回）──
def test_end_to_end_with_real_bridge(sb):
    import sys
    import urllib.request
    port = 5595                      # 测试专用（生产 5599 / 隧道 5598 / 其他测试 5589·5590·5596·5597）
    base = f'http://127.0.0.1:{port}'
    env = dict(sb['env'], ISLAND_BRIDGE_LOG=str(sb['dir'] / 'bridge.log'),
               ISLAND_SETTINGS_FILE=str(sb['dir'] / 'settings.json'),
               ISLAND_KIMI_CRED=str(sb['dir'] / 'no-kimi-cred.json'))
    bridge = subprocess.Popen([sys.executable, str(HOOKS.parent / 'bridge' / 'island_bridge.py'),
                               '--port', str(port)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def api(path, body=None):
        req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    try:
        for _ in range(50):
            try:
                api('/api/health'); break
            except Exception:
                time.sleep(0.2)
        api('/api/session_yolo', {'session_id': 'sY', 'on': True})
        p, dt = run_hook(sb, bash_call('sY'))
        assert json.loads(p.stdout) == ALLOW and dt < 0.5 and queued_ids(sb) == []

        # 慢路径：页面（此处由测试代替）看到卡片即点允许
        sb['env']['ISLAND_HOOK_TIMEOUT'] = '10'
        box = {}

        def clicker():
            t_end = time.time() + 8
            while time.time() < t_end:
                pend = api('/api/state').get('pending') or []
                if pend:
                    api('/api/decision', {'id': pend[0]['id'], 'decision': 'allow'})
                    box['clicked'] = time.monotonic()
                    return
                time.sleep(0.05)
        th = threading.Thread(target=clicker, daemon=True); th.start()
        p, _dt = run_hook(sb, bash_call('sN'))
        done = time.monotonic()
        th.join(2)
        assert json.loads(p.stdout) == ALLOW
        assert 'clicked' in box and done - box['clicked'] < 0.4, \
            f"点允许后 {done - box.get('clicked', done):.2f}s 钩子才返回"
    finally:
        bridge.kill(); bridge.wait()


# ── 钩子脚本必须可执行，且 git 里记为 100755（2026-09-26 事故） ─────────────
# settings.json 按路径直接执行钩子；本仓库 core.fileMode=false 且钩子一直记为 100644，
# 执行位只存在磁盘上——一次合并改写了 pre_tool_use.sh/notify_hook.sh，文件以 644
# 落盘，全部 Claude 会话的审批与通知钩子失效 84 秒（非阻塞报错，岛收不到任何事件）。
def test_hook_scripts_executable_in_git():
    # 从 hooks/ 自身调 git：源码嵌在工作区 apps/ 下，开源镜像在仓根，两处都能定位到仓
    out = subprocess.run(['git', 'ls-files', '-s', '--', str(HOOKS)], cwd=HOOKS,
                         capture_output=True, text=True, check=True).stdout
    modes = {Path(l.split('\t')[1]).name: l.split()[0] for l in out.splitlines()}
    scripts = sorted(n for n in modes if n.endswith('.sh'))
    assert scripts, 'hooks/ 下应有 .sh 脚本'
    bad = [n for n in scripts if modes[n] != '100755']
    assert not bad, f'git 里未记为可执行（合并/检出会丢执行位）：{bad}'
    not_x = [n for n in scripts if not os.access(HOOKS / n, os.X_OK)]
    assert not not_x, f'磁盘上不可执行：{not_x}'


# ── statusline 包装：按会话缓存上下文占用（2026-09-26） ─────────────────────
# Kimi 行早有 ctx N%，Claude 行没有；Claude Code 每轮把 context_window.used_percentage
# 传给 statusline，包装脚本此前只缓存了额度（rate_limits）。
STATUSLINE = HOOKS / 'island_statusline.sh'


def _statusline(sb, payload):
    deleg = sb['dir'] / 'delegate.txt'
    deleg.write_text('cat >/dev/null; echo HUD-OK\n')
    env = dict(sb['env'], ISLAND_RL_CACHE=str(sb['dir'] / 'rl.json'),
               ISLAND_STATUSLINE_DELEGATE_FILE=str(deleg))
    return subprocess.run(['bash', str(STATUSLINE)], input=json.dumps(payload), env=env,
                          capture_output=True, text=True, timeout=20)


def test_statusline_caches_context_pct_per_session(sb):
    sid = '7425bd06-e27d-49ea-8ce9-8846e9c0a742'
    p = _statusline(sb, {'session_id': sid, 'context_window': {'used_percentage': 42.6},
                         'rate_limits': {'five_hour': {'used_percentage': 7}}})
    assert p.stdout.strip() == 'HUD-OK', '须原样转发给原 HUD'
    d = json.loads((sb['dir'] / 'ctx' / f'{sid}.json').read_text())
    assert d['pct'] == 43 and d['ts'] > 0
    assert json.loads((sb['dir'] / 'rl.json').read_text())['five_hour']['used_percentage'] == 7


def test_statusline_ctx_fallbacks_and_guards(sb):
    # 无 session_id 时取会话记录文件名；百分比为空（首轮未计数）不写
    _statusline(sb, {'transcript_path': '/x/abc-123.jsonl', 'context_window': {'used_percentage': 9}})
    assert json.loads((sb['dir'] / 'ctx' / 'abc-123.json').read_text())['pct'] == 9
    _statusline(sb, {'session_id': 'empty-1', 'context_window': {'used_percentage': None}})
    assert not (sb['dir'] / 'ctx' / 'empty-1.json').exists()
    # 会话 id 带路径字符 → 拒写（防路径穿越）
    _statusline(sb, {'session_id': '../../evil', 'context_window': {'used_percentage': 5}})
    assert not list(sb['dir'].rglob('evil*'))


# ── PermissionRequest 钩子：终端权限框同步上岛（2026-09-26） ──────────
# 实测前提（Claude Code 2.1.283 交互式自动模式）：终端框与本钩子并行，谁先答算谁的；
# 终端先答时本钩子收到 SIGTERM；钩子无输出 = 不裁决，终端框照常。
PR = HOOKS / 'permission_request.sh'
PR_ALLOW = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}


def queue_entries(sb):
    q = sb['dir'] / 'queue.jsonl'
    return [json.loads(l) for l in q.read_text().splitlines() if l.strip()] if q.exists() else []


def pr_call(sid='sP', cmd='for id in a b; do rm -f $id/x; done'):
    return {'session_id': sid, 'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash',
            'tool_input': {'command': cmd}, 'permission_mode': 'auto', 'permission_suggestions': [],
            'transcript_path': '/x/sP.jsonl', 'cwd': '/tmp'}


def test_pr_enqueues_and_allow(sb):
    stop = _responder(sb, 'allow')
    try:
        p, dt = run_hook(sb, pr_call(), PR)
    finally:
        stop.set()
    assert json.loads(p.stdout) == PR_ALLOW
    e = queue_entries(sb)[0]
    assert e['id'].startswith('pr_') and e['hook_event_name'] == 'PermissionRequest'
    assert e['tool_name'] == 'Bash' and e['session_id'] == 'sP'
    assert dt < 1.0, f'答复就绪后 {dt:.2f}s 才返回'


def test_pr_deny_carries_message(sb):
    stop = _responder(sb, 'deny', reason='用户在灵动岛上拒绝了')
    try:
        p, _ = run_hook(sb, pr_call(), PR)
    finally:
        stop.set()
    d = json.loads(p.stdout)['hookSpecificOutput']['decision']
    assert d == {'behavior': 'deny', 'message': '用户在灵动岛上拒绝了'}


def test_pr_deny_without_reason_has_default_message(sb):
    stop = _responder(sb, 'deny')
    try:
        p, _ = run_hook(sb, pr_call(), PR)
    finally:
        stop.set()
    d = json.loads(p.stdout)['hookSpecificOutput']['decision']
    assert d['behavior'] == 'deny' and d['message']


def test_pr_timeout_no_output_and_cancels_card(sb):
    sb['env']['ISLAND_PR_TIMEOUT'] = '1'
    p, dt = run_hook(sb, pr_call(), PR)
    assert p.stdout.strip() == '' and p.returncode == 0, '超时不裁决，终端框照常'
    ents = queue_entries(sb)
    assert ents[-1] == {'type': 'cancel', 'id': ents[0]['id']}, '超时须撤掉岛上的卡'


def test_pr_sigterm_cancels_card(sb):
    """终端先答时 Claude Code 结束钩子：写撤卡标记后安静退出。"""
    proc = subprocess.Popen(['bash', str(PR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=sb['env'], text=True)
    proc.stdin.write(json.dumps(pr_call()))
    proc.stdin.close()
    deadline = time.time() + 5
    while time.time() < deadline and not queue_entries(sb):
        time.sleep(0.05)
    time.sleep(0.2)
    proc.terminate()
    proc.wait(timeout=5)
    out = proc.stdout.read()
    assert out.strip() == ''
    ents = queue_entries(sb)
    assert ents[-1] == {'type': 'cancel', 'id': ents[0]['id']}


def test_pr_broken_response_never_allows(sb):
    rd = sb['dir'] / 'responses'
    stop = threading.Event()

    def bad():
        while not stop.is_set():
            for e in queue_entries(sb):
                if e.get('id', '').startswith('pr_') and e.get('type') != 'cancel':
                    rd.mkdir(exist_ok=True)
                    (rd / f"{e['id']}.json").write_text('{"decision": "al')
                    return
            time.sleep(0.02)
    threading.Thread(target=bad, daemon=True).start()
    try:
        p, _ = run_hook(sb, pr_call(), PR)
    finally:
        stop.set()
    assert p.stdout.strip() == '', '读不懂答复就不裁决，决不兜底放行'


def test_pr_ignores_yolo_and_always(sb):
    """YOLO 名单、本会话 Always 都不能让 perm 卡跳过人。"""
    (sb['dir'] / 'yolo_sessions.json').write_text(json.dumps(['sP']))
    (sb['dir'] / 'always_claude').write_text(json.dumps({'agent_source': 'claude', 'session_id': 'sP'}))
    sb['env']['ISLAND_PR_TIMEOUT'] = '1'
    p, _ = run_hook(sb, pr_call(), PR)
    assert p.stdout.strip() == '' and queue_entries(sb)[0]['id'].startswith('pr_')


# ── 终端先答的撤卡：盯会话记录（2026-09-26 真机更正） ──────────────────────────
# 真机端到端实测：终端先答时 Claude Code 并不结束 PermissionRequest 钩子（试做里那次
# SIGTERM 实为关 tmux 会话所致）。弹框挂着时会话记录无新增，作答后第一条新增就是该命令的
# tool_result——据此判定“终端已答”，撤卡退出。只新增附件行（别的钩子记录）不算。
def _start_pr(sb, transcript):
    call = pr_call()
    call['transcript_path'] = str(transcript)
    proc = subprocess.Popen(['bash', str(PR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=sb['env'], text=True)
    proc.stdin.write(json.dumps(call))
    proc.stdin.close()
    deadline = time.time() + 5
    while time.time() < deadline and not queue_entries(sb):
        time.sleep(0.05)
    return proc


def _tu(tid):
    return json.dumps({'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': pr_call()['tool_input']}]}},
        separators=(',', ':')) + '\n'          # 与 Claude Code 会话记录同为紧凑 JSON


def _tr(tid):
    return json.dumps({'type': 'user', 'message': {'content': [
        {'type': 'tool_result', 'tool_use_id': tid, 'content': 'loop-done'}]}},
        separators=(',', ':')) + '\n'


def test_pr_terminal_answer_detected_via_transcript(sb):
    """同一命令以前跑过（old1 已完成）不得误判；本次调用的助手行晚于钩子才落盘（缓冲）也不得误判；
    本次调用的 tool_result 出现才算终端已答。"""
    tr = sb['dir'] / 'sess.jsonl'
    tr.write_text(_tu('old1') + _tr('old1'))
    proc = _start_pr(sb, tr)
    time.sleep(0.3)
    with open(tr, 'a') as f:        # 缓冲晚写：本次调用的助手行在钩子开始后才落盘
        f.write(_tu('new1'))
        f.write('{"type":"attachment","attachment":{"type":"hook_success","hookName":"PreToolUse:Bash"}}\n')
    time.sleep(1.2)
    assert proc.poll() is None, '助手行/附件晚写不是终端作答，不得撤卡'
    with open(tr, 'a') as f:        # 终端里按了 Yes：本次调用执行完毕
        f.write(_tr('new1'))
    t0 = time.time()
    proc.wait(timeout=5)
    assert time.time() - t0 < 1.5, '终端作答后应在 1.5s 内撤卡退出'
    assert proc.stdout.read().strip() == ''
    ents = queue_entries(sb)
    assert ents[-1] == {'type': 'cancel', 'id': ents[0]['id']}


def test_pr_attachment_only_does_not_cancel(sb):
    tr = sb['dir'] / 'sess.jsonl'
    tr.write_text('{"type":"assistant"}\n')
    proc = _start_pr(sb, tr)
    time.sleep(0.4)
    with open(tr, 'a') as f:        # 只是别的钩子写了附件，会话并没往下走
        f.write('{"type":"attachment","attachment":{"type":"hook_success","hookName":"PreToolUse:Bash"}}\n')
    time.sleep(1.2)
    alive = proc.poll() is None
    proc.terminate()
    proc.wait(timeout=5)
    assert alive, '只有附件行不得撤卡'


# ── 代码审查修复（2026-09-26）─────────────────────────────────────────────
def test_pr_skips_ask_and_plan(sb):
    """审查#1：选择题/计划由 PreToolUse 走岛上作答；本钩子不得再发一张只有允许/拒绝的卡。"""
    for tool in ('AskUserQuestion', 'ExitPlanMode'):
        call = pr_call()
        call['tool_name'] = tool
        p, _ = run_hook(sb, call, PR)
        assert p.stdout.strip() == '' and p.returncode == 0
    assert queue_entries(sb) == []


def test_pr_ids_unique_for_identical_requests(sb):
    """审查#2：同一秒内两个一模一样的请求不得撞 id（否则一次点击答两个、或一个干等 110s）。"""
    sb['env']['ISLAND_PR_TIMEOUT'] = '1'
    procs = [subprocess.Popen(['bash', str(PR)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              env=sb['env'], text=True) for _ in range(2)]
    for p in procs:
        p.stdin.write(json.dumps(pr_call()))
        p.stdin.close()
    for p in procs:
        p.wait(timeout=10)
    ids = {e['id'] for e in queue_entries(sb) if e.get('type') != 'cancel'}
    assert len(ids) == 2, ids
    assert all(i.rsplit('_', 1)[-1].isdigit() for i in ids), 'id 末段须仍是入队秒数（桥按它算回放窗口）'


def test_pr_entry_tagged_for_bridge(sb):
    """审查#3：钩子自己打 perm 标记，桥据此判定，不依赖来源名（防误配成 codex 时漏网）。"""
    sb['env']['ISLAND_PR_TIMEOUT'] = '1'
    sb['env']['ISLAND_AGENT_SOURCE'] = 'codex'
    run_hook(sb, pr_call(), PR)
    e = queue_entries(sb)[0]
    assert e.get('island_perm') == 1


def test_pr_writes_done_marker_after_decision(sb):
    """审查#5：岛上答完正常退出也写撤卡标记，桥重启回放不会让答过的卡复活。"""
    stop = _responder(sb, 'allow')
    try:
        p, _ = run_hook(sb, pr_call(), PR)
    finally:
        stop.set()
    assert json.loads(p.stdout) == PR_ALLOW
    ents = queue_entries(sb)
    assert ents[-1] == {'type': 'cancel', 'id': ents[0]['id']}
