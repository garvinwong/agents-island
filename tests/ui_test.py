#!/usr/bin/env python3
"""Agents Island — Playwright UI 自测（浏览器模式，隔离沙箱桥）。

覆盖 RISKS.md 用例 T4/T5/T6/T8/T12 的 UI 侧。
运行：cd apps/agents-island && python3 tests/ui_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
PORT = 5596
BASE = f'http://127.0.0.1:{PORT}'

passed, failed = [], []


def check(name, cond, detail=''):
    (passed if cond else failed).append(name)
    print(f'  {"✅" if cond else "❌"} {name}' + (f' — {detail}' if detail and not cond else ''))


def enqueue(payload):
    req = urllib.request.Request(f'{BASE}/api/test/enqueue',
                                 data=json.dumps(payload).encode(), method='POST')
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())['id']


def wait_mode(page, mode, timeout=12000):
    page.wait_for_function(f'window.__island.mode === "{mode}"', timeout=timeout)


def main():
    tmp = tempfile.mkdtemp(prefix='island_ui_')
    resp_dir = Path(tmp) / 'responses'
    # 状态目录/桥日志/Kimi 凭证也进沙箱（此前漏隔离，会写生产桥日志）
    env = dict(os.environ,
               ISLAND_STATE_DIR=tmp,
               ISLAND_BRIDGE_LOG=str(Path(tmp) / 'bridge.log'),
               ISLAND_KIMI_CRED=str(Path(tmp) / 'no-kimi-cred.json'),
               ISLAND_QUEUE_FILE=str(Path(tmp) / 'queue.jsonl'),
               ISLAND_RESP_DIR=str(resp_dir),
               ISLAND_ALWAYS_CLAUDE=str(Path(tmp) / 'always_claude'),
               ISLAND_ALWAYS_CODEX=str(Path(tmp) / 'always_codex'),
               ISLAND_RL_CACHE=str(Path(tmp) / 'rl.json'),
               ISLAND_SETTINGS_FILE=str(Path(tmp) / 'settings.json'))
    bridge = subprocess.Popen([sys.executable, str(ROOT / 'bridge' / 'island_bridge.py'),
                               '--port', str(PORT), '--debug'],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f'{BASE}/api/health', timeout=2)
                break
            except Exception:
                time.sleep(0.2)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 560, 'height': 620})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'{BASE}/?poll=200')
            page.wait_for_timeout(800)

            print('— T4 四态切换 —')
            check('初始 sliver', page.evaluate('window.__island.mode') == 'sliver')
            page.hover('#island')
            wait_mode(page, 'compact')
            check('hover → compact', True)
            page.click('#island')
            wait_mode(page, 'expanded')
            check('click → expanded', True)
            check('expanded 渲染会话区', page.locator('#ex-body').inner_html() != '')
            stats = page.locator('#ex-stats').text_content()
            check('stats 文本含 live/working', 'live' in stats and 'working' in stats, stats)
            page.keyboard.press('Escape')
            wait_mode(page, 'sliver')
            check('Esc → sliver', True)

            print('— T5 审批弹出 + 快捷键 —')
            eid = enqueue({'tool_name': 'Bash', 'tool_input': {'command': 'rm -rf /tmp/x'}})
            wait_mode(page, 'approval')
            check('审批自动弹出 approval', True)
            check('工具名显示', page.locator('#ap-tool').text_content() == 'Bash')
            check('详情显示命令', 'rm -rf' in page.locator('#ap-detail').text_content())
            page.keyboard.press('a')
            page.wait_for_timeout(700)
            resp = resp_dir / f'{eid}.json'
            check('按 A → 响应文件 allow',
                  resp.exists() and json.loads(resp.read_text())['decision'] == 'allow')
            resp.unlink(missing_ok=True)
            wait_mode(page, 'compact')
            check('审批毕回 compact', True)
            page.mouse.move(500, 560)   # 鼠标离岛，让自动缩回生效
            wait_mode(page, 'sliver', timeout=12000)
            check('2.5s 后自动缩回 sliver', True)

            print('— T12 多条排队 —')
            ids = [enqueue({'tool_name': f'Tool{i}', 'tool_input': {'command': f'cmd{i}'}})
                   for i in range(3)]
            wait_mode(page, 'approval')
            # 三条可能分两轮轮询才到齐：等徽数到位再判（原固定等 600ms，偶发红）
            try:
                page.wait_for_function("document.getElementById('ap-queue').textContent === '1 / 3'",
                                       timeout=5000)
            except Exception:
                pass
            check('队列徽数 1 / 3', page.locator('#ap-queue').text_content() == '1 / 3')
            # 逐张等答复文件落地再按下一个键（原固定等 600ms：机器忙时换卡动画未完，
            # 下一个键被忽略，第三张卡没有答复而崩溃）
            def press_and_wait(key, eid, timeout=5):
                page.keyboard.press(key)
                deadline = time.time() + timeout
                while time.time() < deadline and not (resp_dir / f'{eid}.json').exists():
                    page.wait_for_timeout(100)
                page.wait_for_timeout(400)          # 换卡动画与键盘锁放开
                f = resp_dir / f'{eid}.json'
                return json.loads(f.read_text())['decision'] if f.exists() else None
            check('Deny 响应', press_and_wait('d', ids[0]) == 'deny')
            press_and_wait('a', ids[1])
            dec = press_and_wait('s', ids[2])
            check('Always 写标志', (Path(tmp) / 'always_claude').exists())
            check('Always 响应 allow', dec == 'allow', str(dec))
            (Path(tmp) / 'always_claude').unlink(missing_ok=True)
            for i in ids:
                (resp_dir / f'{i}.json').unlink(missing_ok=True)

            print('— T5b 按钮点击路径 —')
            eid = enqueue({'tool_name': 'Write', 'tool_input': {'file_path': '/tmp/t.txt'}})
            wait_mode(page, 'approval')
            page.wait_for_timeout(400)
            page.click('#btn-deny')
            page.wait_for_timeout(700)
            check('点击 Deny 按钮',
                  json.loads((resp_dir / f'{eid}.json').read_text())['decision'] == 'deny')
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)

            print('— T6 expanded 内联审批 —')
            page.hover('#island'); wait_mode(page, 'compact')
            page.click('#island'); wait_mode(page, 'expanded')
            eid = enqueue({'tool_name': 'Edit', 'tool_input': {'file_path': '/tmp/e.txt'}})
            page.wait_for_timeout(800)
            check('expanded 不被抢占', page.evaluate('window.__island.mode') == 'expanded')
            check('内联审批卡出现', page.locator('.pend-card').count() == 1)
            page.click('.pend-card .btn-allow')
            page.wait_for_timeout(700)
            check('内联 Allow 生效',
                  json.loads((resp_dir / f'{eid}.json').read_text())['decision'] == 'allow')
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)

            print('— 通知 toast —')
            enqueue({'id': f'notify_{time.time_ns()}', 'type': 'notify',
                     'hook_event_name': 'stop', 'agent_source': 'claude'})
            page.wait_for_timeout(900)
            check('toast 出现', page.locator('.toast-item').count() >= 1)
            page.keyboard.press('Escape')

            print('— 通知分类（idle_prompt 静音 / 终端等批准醒目 / Stop 显示结果首句）—')
            qf = Path(tmp) / 'queue.jsonl'

            def put_notify(**kw):
                entry = {'id': f'notify_{time.time_ns()}', 'type': 'notify',
                         'agent_source': 'claude', **kw}
                with open(qf, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')

            def capsule():
                return page.evaluate('window.__island.state.toastMsg?.text || ""')

            # 生产窗口（body.native）通知走胶囊内联；此处切到该路径验真实文案
            page.evaluate("document.body.classList.add('native')")
            base = capsule()
            put_notify(hook_event_name='Notification', notification_type='idle_prompt',
                       message='Claude is waiting for your input')
            page.wait_for_timeout(1200)
            check('idle_prompt 不再弹（与 Stop 同一件事）', capsule() == base, capsule())
            put_notify(hook_event_name='Notification', notification_type='permission_prompt',
                       message='Claude needs your permission')
            page.wait_for_timeout(1200)
            check('permission_prompt 醒目提示（⚠，不打 ✓）', capsule().startswith('⚠'), capsule())
            put_notify(hook_event_name='Stop',
                       last_assistant_message='已提交主树，提交号 3491233e0。另外还顺手改了两处。')
            page.wait_for_timeout(1200)
            check('Stop 显示结果首句',
                  '已提交主树，提交号 3491233e0' in capsule() and '另外还顺手' not in capsule(),
                  capsule())
            page.evaluate("document.body.classList.remove('native')")

            print('— PermissionRequest 卡—')
            page.evaluate("document.body.classList.add('native')")
            pr_id = f'pr_ui_{time.time_ns()}'
            with open(qf, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'id': pr_id, 'hook_event_name': 'PermissionRequest', 'session_id': 'sess-perm',
                                    'tool_name': 'Bash', 'tool_input': {'command': 'rm -f $id/$f'},
                                    'island_perm': 1}) + '\n')   # 与真钩子一致：桥认此标记判 perm
            wait_mode(page, 'approval')
            page.wait_for_timeout(500)
            pc = page.evaluate("""() => ({
              always: document.getElementById('btn-always').offsetParent !== null,
              tag: document.getElementById('ap-timer').textContent,
              want: window.__island.state && (window.__island.permTag || ''),
              toast: window.__island.state.toastMsg?.text || ''})""")
            check('perm 卡不给 Always 键', pc['always'] is False, str(pc))
            check('perm 卡头标“终端也在等你”', bool(pc['tag']) and pc['tag'] == pc['want'], str(pc))
            before_toast = pc['toast']
            put_notify(hook_event_name='Notification', notification_type='permission_prompt',
                       session_id='sess-perm', message='Claude needs your permission')
            page.wait_for_timeout(900)
            check('已有 perm 卡时不再弹“终端在等你批准”',
                  page.evaluate('window.__island.state.toastMsg?.text || ""') == before_toast)
            page.keyboard.press('s')
            page.wait_for_timeout(700)
            check('perm 卡上按 S 无效', not (resp_dir / f'{pr_id}.json').exists()
                  and page.evaluate('window.__island.mode') == 'approval')
            page.keyboard.press('a')
            page.wait_for_timeout(700)
            r = resp_dir / f'{pr_id}.json'
            check('perm 卡按 A 允许', r.exists() and json.loads(r.read_text())['decision'] == 'allow')
            r.unlink(missing_ok=True)
            page.evaluate("document.body.classList.remove('native')")
            page.mouse.move(500, 560)

            print('— 会话状态小胶囊 / 胶囊实时活动（Owner 09-26 定：不加分组标题）—')
            res = page.evaluate("""() => {
              const I = window.__island; if (!I.sessionState) return null;
              const now = 100000;
              const base = {session_id: 's', status: 'standby', age_seconds: 300};
              const ctx = (o = {}) => Object.assign({now, pending: [], termWait: {}, seen: {}}, o);
              return {
                active: I.sessionState({...base, status: 'executing_tool', age_seconds: 5}, ctx()),
                reply: I.sessionState(base, ctx()),
                seen: I.sessionState(base, ctx({seen: {s: now}})),
                newTurnAfterSeen: I.sessionState({...base, age_seconds: 10}, ctx({seen: {s: now - 100}})),
                old: I.sessionState({...base, age_seconds: 9000}, ctx()),
                sub: I.sessionState({...base, subagent: true}, ctx()),
                needPending: I.sessionState({...base, status: 'executing_tool'},
                                            ctx({pending: [{id: 'x', session_id: 's'}]})),
                needTerm: I.sessionState(base, ctx({termWait: {s: now - 200}})),
                termAnswered: I.sessionState({...base, age_seconds: 50}, ctx({termWait: {s: now - 200}})),
              };
            }""")
            want = {'active': 'active', 'reply': 'reply', 'seen': 'idle', 'newTurnAfterSeen': 'reply',
                    'old': 'idle', 'sub': 'idle', 'needPending': 'need', 'needTerm': 'need',
                    'termAnswered': 'reply'}
            check('会话状态判定（需要你/进行中/待回复/空闲）', res == want, str(res))
            cap = page.evaluate("""() => {
              const I = window.__island; if (!I.compactActivity) return null;
              const ctx = {now: 100000, pending: [], termWait: {}, seen: {}};
              const c = {session_id: 'c', is_live: true, status: 'standby', age_seconds: 1440, title: '中日调研'};
              const sess = {claude: [
                {session_id: 'b', is_live: true, status: 'executing_tool', age_seconds: 90, title: '作业', last_tool: 'Edit'},
                {session_id: 'a', is_live: true, status: 'executing_tool', age_seconds: 27, title: '灵动岛项目优化', last_tool: 'Bash'},
                c]};
              const strip = h => (h || '').replace(/<[^>]+>/g, '');
              const one = I.compactActivity(sess, ctx), idle = I.compactActivity({claude: [c]}, ctx),
                    none = I.compactActivity({claude: [{...c, age_seconds: 9000}]}, ctx);
              // 轮播顺序：需要你 → 待回复 → 进行中（同状态按最近）；空闲不参与
              const mix = I.compactActivity({claude: [...sess.claude,
                {session_id: 'n', is_live: true, status: 'waiting_permission', age_seconds: 300, title: '待批'},
                {session_id: 'z', is_live: true, status: 'idle', age_seconds: 9000, title: '闲置'}]}, ctx);
              return {text: strip(one.html), replies: one.replies, idle: strip(idle.html), none: none.html,
                      order: (mix.items || []).map(x => x.s.title), texts: (mix.items || []).map(x => strip(x.html))};
            }""")
            check('胶囊帧不再拼“另 N 个”，进行中帧=会话名 · 工具 · 时长',
                  bool(cap) and len(cap['texts']) == 4
                  and cap['texts'][2].startswith('灵动岛项目优化 · Bash · 27s')
                  and not any('另' in x or '+1' in x for x in cap['texts'] + [cap['text']]),
                  str(cap))
            check('轮播顺序：需要你 → 待回复 → 进行中（同状态按最近），空闲不参与',
                  bool(cap) and cap['order'] == ['待批', '中日调研', '灵动岛项目优化', '作业'], str(cap))
            check('需要你帧写明“需要你”、待回复帧写明“等你回复”',
                  bool(cap) and len(cap['texts']) == 4
                  and cap['texts'][0].endswith(('需要你', 'Needs you'))
                  and cap['texts'][1].endswith(('等你回复', 'your turn')), str(cap))
            check('胶囊待回复计数 + 无进行中时显示待回复会话',
                  bool(cap) and cap['replies'] == 1 and cap['idle'].startswith('中日调研'), str(cap))
            check('无进行中也无待回复 → 回落原文案', bool(cap) and cap['none'] is None, str(cap))
            page.hover('#island'); wait_mode(page, 'compact')
            page.click('#island'); wait_mode(page, 'expanded')
            # 沙箱桥首次全量扫描会话记录要几秒，等到有会话行再量
            page.wait_for_function("document.querySelectorAll('#ex-body .row').length > 0", timeout=15000)
            pills = page.evaluate("""() => [...document.querySelectorAll('#ex-body .row')]
                .map(r => r.querySelectorAll('.st-pill').length)""")
            check('每个会话行恰好一个状态胶囊', bool(pills) and all(n == 1 for n in pills), str(pills))
            page.wait_for_timeout(1200)   # 行到齐后的下一轮渲染会按内容量重设高度
            ov = page.evaluate("""() => { const b = document.getElementById('ex-body');
                return {over: b.scrollHeight - b.clientHeight,
                        h: document.getElementById('island').offsetHeight}; }""")
            check('展开高度按内容量：未到上限不出滚动条', ov['over'] <= 1 or ov['h'] >= 480, str(ov))
            gs = page.evaluate("""() => { const I = window.__island, b = document.getElementById('ex-body');
                const h0 = I.measureExpandedHeight(), d = document.createElement('div');
                d.style.height = '60px'; b.appendChild(d); const h1 = I.measureExpandedHeight();
                d.remove(); const h2 = I.measureExpandedHeight(); return {h0, h1, h2}; }""")
            check('内容增减时高度跟着变（能缩回）',
                  gs['h2'] == gs['h0'] and (gs['h1'] - gs['h0'] == 60 or gs['h1'] == 480), str(gs))
            hint = page.evaluate("""() => {
              const I = window.__island; if (!I.jumpHint) return null;
              const el = document.getElementById('foot-hint'), before = el.textContent;
              I.jumpHint('focused', '作业'); const same = el.textContent === before;
              I.jumpHint('terminal', '作业'); const tab = el.textContent;
              I.jumpHint('notfound', ''); const none = el.textContent;
              return {same, tab, none, before};
            }""")
            ub = page.evaluate("""() => {
              const I = window.__island; if (!I.fmtLeft) return null;
              const s = I.state, keepU = s.usage, keepT = s.bridgeTs, now = 1790000000;
              s.bridgeTs = now;
              s.usage = {five_hour: {used_percentage: 27, resets_at: now + 2 * 3600 + 14 * 60},
                         seven_day: {used_percentage: 69, resets_at: now + 2.6 * 86400},
                         kimi: {five_hour: {used_percentage: 7, resets_at: '2026-09-01T00:00:00.000583Z'},
                                seven_day: {used_percentage: 1, resets_at: new Date((now + 3600) * 1000).toISOString()}}};
              const claude = I.usageBars('claude'), kimi = I.usageBars('kimi');
              s.usage = keepU; s.bridgeTs = keepT;
              const txt = h => h.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
              return {claude: txt(claude), kimi: txt(kimi), tip: /title="[^"]*2\\.6d/.test(claude),
                      fl: [I.fmtLeft(600), I.fmtLeft(2 * 3600 + 14 * 60), I.fmtLeft(2.6 * 86400)]};
            }""")
            check('额度条重置倒计时（过期不显示）',
                  bool(ub) and '↻2h14m' in ub['claude'] and '↻2.6d' in ub['claude'] and ub['tip']
                  and ub['kimi'].count('↻') == 1 and ub['fl'] == ['10m', '2h14m', '2.6d'], str(ub))
            check('跳转结果提示：标签页名 / 没找到 / 已聚焦不提示',
                  bool(hint) and hint['same'] and '作业' in hint['tab']
                  and hint['none'] not in (hint['before'], hint['tab']), str(hint))
            page.keyboard.press('Escape')
            tf = page.evaluate("""() => window.__island.toastOf?.({hook_event_name: 'Stop',
                title: '灵动岛项目优化', last_assistant_message: '改好了。其余不变'})?.text""")
            check('胶囊文案 = 会话名 · 结果首句', tf == '✓ 灵动岛项目优化 · 改好了。', str(tf))
            page.keyboard.press('Escape')

            print('— 细条会话刻度（Owner 09-26 选 A）—')
            tk = page.evaluate("""() => { const I = window.__island; if (!I.sliverTickModel) return null;
              const ctx = {now: 100000, termWait: {}, seen: {},
                           pending: [{id: 'p', session_id: 't-c'}, {id: 'q', session_id: null}]};
              const cl = [
                {session_id: 't-a', is_live: true, status: 'executing_tool', age_seconds: 5},
                {session_id: 't-b', is_live: true, status: 'standby', age_seconds: 300},
                {session_id: 't-c', is_live: true, status: 'standby', age_seconds: 300},
                {session_id: 't-d', is_live: true, status: 'standby', age_seconds: 9000},
                {session_id: 't-x', is_live: false, status: 'standby', age_seconds: 10}];
              const cx = [{session_id: 't-e', is_live: true, status: 'executing_tool', age_seconds: 2}];
              const f = m => m.map(t => t.sid + ':' + t.st);
              const m1 = f(I.sliverTickModel({claude: cl, codex: cx}, ctx));
              const m2 = f(I.sliverTickModel({claude: [cl[3], cl[2], cl[1], cl[0]], codex: cx}, ctx));
              return {m1, m2}; }""")
            want = ['t-a:active', 't-b:reply', 't-c:need', 't-d:idle', 't-e:active', '_pending:need']
            check('细条刻度：一会话一格、颜色=状态、孤儿待批补格', bool(tk) and tk['m1'] == want, str(tk))
            check('细条刻度：顺序固定，不随活跃度跳动', bool(tk) and tk['m2'] == want, str(tk))
            tw = page.evaluate("""() => { const I = window.__island; if (!I.sliverTickWidth) return null;
              return [1,2,3,4,5,6,7,8,9,10,12,16,20].map(n => [n, I.sliverTickWidth(n)]); }""")
            ws = [w for _, w in tw] if tw else []
            span = {n: n * w + 3 * (n - 1) for n, w in (tw or [])}     # 刻度总长；细条宽 220
            check('刻度长度：1 个 80、2 个 44；6 个占细条约 80%、8 个约 90%，再多保持 90% 内、每格变短',
                  bool(tw) and ws[0] == 80 and ws[1] == 44 and all(a > b for a, b in zip(ws, ws[1:]))
                  and min(ws) >= 5 and 0.76 <= span[6] / 220 <= 0.82 and 0.87 <= span[8] / 220 <= 0.91
                  and all(0.84 <= span[n] / 220 <= 0.905 for n in (9, 10, 12, 16, 20)), str(tw))
            page.mouse.move(500, 560); wait_mode(page, 'sliver'); page.wait_for_timeout(700)
            dom = page.evaluate("""() => ({ticks: document.querySelectorAll('#sliver-ticks .tick').length,
              live: Object.values(window.__island.state.sessions || {}).flat().filter(s => s.is_live).length,
              pend: window.__island.state.pending.length})""")
            th = page.evaluate("""() => { document.body.classList.add('native');
              const t = document.querySelector('#sliver-ticks .tick');
              const h = t ? getComputedStyle(t).height : null;
              document.body.classList.remove('native'); return h; }""")
            check('刻度高 4px（与旧玻璃棒同高，Owner 09-26 要求加高）', th == '4px', str(th))
            # sliverPulse 只在细条态生效：鼠标若还停在岛上（胶囊态）会直接返回，先移开等回细条（偶发红根因）
            page.mouse.move(5, 600); wait_mode(page, 'sliver')
            br = page.evaluate("""() => { const I = window.__island, box = document.getElementById('sliver-ticks');
              if (!I.sliverPulse || !box) return null;
              const t = document.createElement('span'); t.className = 'tick reply'; box.appendChild(t);
              I.sliverPulse();
              const cs = getComputedStyle(t);
              const r = {breathe: box.classList.contains('breathe'), name: cs.animationName,
                         count: cs.animationIterationCount, fn: cs.animationTimingFunction};
              t.remove(); return r; }""")
            check('待回复白格呼吸：单次播放、步进帧（非常驻动画，守性能定律）',
                  bool(br) and br['breathe'] and br['name'] == 'tick-breathe' and br['count'] == '1'
                  and 'steps' in br['fn'], str(br))
            check('细条渲染格数 = 在线会话数', dom['ticks'] == dom['live'] + (1 if dom['pend'] else 0) and dom['live'] > 0,
                  str(dom))

            print('— 真机形态：窗口还是细条宽时展开，只调一次尺寸 —')
            # 组头要有额度条+重置倒计时（真机形态），窄宽度下才会折行、暴露测量宽度问题
            now = time.time()
            (Path(tmp) / 'rl.json').write_text(json.dumps({
                'five_hour': {'used_percentage': 13, 'resets_at': int(now + 3.8 * 3600)},
                'seven_day': {'used_percentage': 71, 'resets_at': int(now + 2.3 * 86400)}}))
            p2 = browser.new_page(viewport={'width': 220, 'height': 36})
            p2.add_init_script("""window.__rs = []; window.pywebview = {api: {
                resize_for: (m, h) => { window.__rs.push([m, h]); return Promise.resolve(true); },
                set_interactive() {}, surface_alert() {}, set_working() {}, set_panel_alpha() {},
                is_autostart: () => false }};""")
            p2.goto(f'{BASE}/?poll=300&lang=zh')
            p2.evaluate("document.body.classList.add('native')")
            p2.wait_for_function("Object.values(window.__island.state.sessions||{}).flat().some(s=>s.is_live)",
                                 timeout=20000)
            p2.wait_for_function("!!(window.__island.state.usage||{}).seven_day", timeout=15000)
            p2.wait_for_timeout(400)
            # 展开与“按展开宽度真实排版”在同一次同步执行里完成：setMode 的首次 resize
            # 在第一个 await 之前同步发出，二者之间内容不可能变化
            r = p2.evaluate("""() => {
              window.__rs = [];
              window.__island.setMode('expanded');
              const first = (window.__rs.find(x => x[0] === 'expanded') || [])[1];
              // 同一时刻按展开宽度真实排版量一次（不经被测函数），排除会话内容变化干扰
              const isl = document.getElementById('island'), keep = isl.style.cssText;
              const tw = parseFloat(getComputedStyle(document.getElementById('stage'))
                .getPropertyValue('--w-expanded')) || 478;
              isl.style.setProperty('width', tw + 'px', 'important');
              const f = document.querySelector('.face-expanded'), cs = getComputedStyle(f);
              let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
              for (const el of f.children) {
                const c = getComputedStyle(el); if (c.display === 'none') continue;
                if (el.id === 'ex-body') {
                  h += parseFloat(c.paddingTop) + parseFloat(c.paddingBottom);
                  for (const ch of el.children) { const cc = getComputedStyle(ch);
                    if (cc.position !== 'absolute') h += ch.offsetHeight + parseFloat(cc.marginTop) + parseFloat(cc.marginBottom); }
                } else h += el.offsetHeight + parseFloat(c.marginTop) + parseFloat(c.marginBottom);
              }
              isl.style.cssText = keep;
              return {first, truth: Math.max(200, Math.min(480, Math.ceil(h))), vw: innerWidth};
            }""")
            check('窄窗口下展开首次就按展开宽度定高（不先大后缩）',
                  r['first'] is not None and abs(r['first'] - r['truth']) <= 1, str(r))
            p2.close()

            print('— 胶囊轮播（Owner 09-26：多会话轮流显示、需要你优先，每 2.5 秒一换，右侧 1/N）—')
            def cap_state():
                mk = lambda sid, title, st, age, tool='Bash': {
                    'session_id': sid, 'slug': sid, 'title': title, 'status': st, 'last_tool': tool,
                    'age_seconds': age, 'is_live': True, 'project': 'demo', 'cwd': '/tmp/demo', 'source': 'claude'}
                return {'pending': [], 'notify': [], 'remotes': [], 'show': [], 'usage': {},
                        'sessions': {'claude': [
                            mk('s-act-old', '升级依赖', 'executing_tool', 9),
                            mk('s-reply', '补单元测试', 'idle', 95),
                            mk('s-act-new', '暗色模式', 'executing_tool', 2, 'Edit'),
                            mk('s-need', '修复导入乱码', 'waiting_permission', 4),
                            mk('s-idle', '整理截图', 'idle', 9000)]},
                        'stats': {'decisions': 0, 'uptime': 1}, 'ui': {'cursor_inside': False},
                        'muted': False, 'night': False, 'auto_allow_timeout': 0, 'yolo_sessions': [],
                        'lang': 'zh', 'panel_alpha': 1.0, 'ts': time.time(), 'rev': 'cap-demo'}
            p3 = browser.new_page(viewport={'width': 560, 'height': 300})
            p3.route('**/api/state*', lambda route: route.fulfill(
                status=200, content_type='application/json',
                body=json.dumps(cap_state(), ensure_ascii=False)))
            p3.goto(f'{BASE}/?poll=300&lang=zh')
            p3.wait_for_function("Object.values(window.__island.state.sessions||{}).flat().length === 5",
                                 timeout=10000)
            cap_js = """() => ({text: document.getElementById('compact-text').textContent,
                idx: (document.querySelector('#compact-dots .cap-idx') || {}).textContent || '',
                anim: getComputedStyle(document.getElementById('compact-text')).animationIterationCount,
                name: getComputedStyle(document.getElementById('compact-text')).animationName})"""
            p3.hover('#island'); wait_mode(p3, 'compact'); p3.wait_for_timeout(300)
            f1 = p3.evaluate(cap_js)
            p3.wait_for_timeout(1900); f1b = p3.evaluate(cap_js)   # 进入后约 2.2s：2.5s 间隔下仍是首帧
            check('轮播间隔 2.5 秒：2.2 秒时仍停在首帧', f1b['idx'] == '1/4', str(f1b))
            p3.wait_for_timeout(700)
            f2 = p3.evaluate(cap_js)   # 进入后约 2.9s（换帧在 2.5s、5s）
            p3.wait_for_timeout(2500); f3 = p3.evaluate(cap_js)
            check('轮播首帧=需要你，右侧 1/4（空闲不计）',
                  f1['text'].startswith('修复导入乱码') and f1['idx'] == '1/4', str(f1))
            check('2.5 秒后换到待回复，序号 2/4', f2['text'].startswith('补单元测试') and f2['idx'] == '2/4', str(f2))
            check('再 2.5 秒换到最近在动的会话，序号 3/4',
                  f3['text'].startswith('暗色模式') and f3['idx'] == '3/4', str(f3))
            check('换帧有淡入且只播一次（非常驻动画，守性能定律）',
                  f3['name'] == 'cap-in' and f3['anim'] == '1', str(f3))
            p3.keyboard.press('Escape'); wait_mode(p3, 'sliver')
            check('离开胶囊即停轮播', p3.evaluate('window.__island.capTimerOn') is False)
            p3.mouse.move(5, 250); p3.hover('#island'); wait_mode(p3, 'compact'); p3.wait_for_timeout(300)
            f4 = p3.evaluate(cap_js)
            check('再次进入从最优先的开始', f4['text'].startswith('修复导入乱码') and f4['idx'] == '1/4', str(f4))
            p3.close()

            print('— T7 岛上作答（AskUserQuestion）—')
            eid = enqueue({'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [{
                'question': '选择部署方式？', 'header': '部署',
                'options': [{'label': '蓝绿部署', 'description': '零停机'},
                            {'label': '滚动更新', 'description': '逐批替换'}],
                'multiSelect': False}]}})
            wait_mode(page, 'approval')
            page.wait_for_timeout(500)
            check('ask 渲染选项按钮', page.locator('.ask-opt').count() == 2)
            check('普通按钮隐藏', not page.locator('#ap-actions').is_visible())
            # 2026-09-04 回归：果冻 hover 放大 1.02 曾把 .ask-box 撑出横向滚动条（岛体闪烁）
            page.hover('.ask-opt >> nth=1')
            page.wait_for_timeout(450)
            ov = page.evaluate("""() => { const b = document.querySelector('.ask-box');
                return { x: b.scrollWidth - b.clientWidth, y: b.scrollHeight - b.clientHeight }; }""")
            check('hover 选项不撑出滚动条', ov['x'] <= 0 and ov['y'] <= 0, str(ov))
            page.click('.ask-opt >> nth=0')
            page.wait_for_timeout(700)
            r = json.loads((resp_dir / f'{eid}.json').read_text())
            check('选项→deny+reason', r['decision'] == 'deny' and '蓝绿部署' in r.get('reason', ''))
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)
            wait_mode(page, 'sliver')

            eid = enqueue({'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [{
                'question': '输入分支名？', 'header': '分支',
                'options': [{'label': 'main'}], 'multiSelect': False}]}})
            wait_mode(page, 'approval')
            page.wait_for_timeout(500)
            page.fill('#ask-input', 'feature/island-v2')
            page.keyboard.press('Enter')
            page.wait_for_timeout(700)
            r = json.loads((resp_dir / f'{eid}.json').read_text())
            check('自定义输入→reason 透传',
                  r['decision'] == 'deny' and 'feature/island-v2' in r.get('reason', ''))
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)

            eid = enqueue({'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [{
                'question': 'Q3？', 'options': [{'label': 'X'}], 'multiSelect': False}]}})
            wait_mode(page, 'approval')
            page.wait_for_timeout(500)
            page.click('#ask-terminal')
            page.wait_for_timeout(700)
            r = json.loads((resp_dir / f'{eid}.json').read_text())
            check('改终端回答→allow', r['decision'] == 'allow')
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)
            page.keyboard.press('Escape')

            print('— T9 P0/P1：plan 审阅 / diff / usage / 勿扰 —')
            # plan 审阅：驳回+反馈透传
            eid = enqueue({'tool_name': 'ExitPlanMode', 'tool_input': {
                'plan': '# 实施计划\n\n## 步骤\n- 改 A 文件\n- 跑测试\n\n**风险**：无'}})
            wait_mode(page, 'approval')
            page.wait_for_timeout(500)
            check('plan Markdown 渲染', page.locator('.plan-md h3').count() >= 1)
            page.fill('#plan-feedback', '先补充回滚方案')
            page.click('#plan-reject')
            page.wait_for_timeout(700)
            r = json.loads((resp_dir / f'{eid}.json').read_text())
            check('plan 驳回+意见透传', r['decision'] == 'deny' and '回滚方案' in r.get('reason', ''))
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)
            wait_mode(page, 'sliver')

            # plan 批准 = allow
            eid = enqueue({'tool_name': 'ExitPlanMode', 'tool_input': {'plan': '# P2'}})
            wait_mode(page, 'approval'); page.wait_for_timeout(400)
            page.click('#plan-approve'); page.wait_for_timeout(700)
            check('plan 批准→allow',
                  json.loads((resp_dir / f'{eid}.json').read_text())['decision'] == 'allow')
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)

            # Edit diff 红绿行
            eid = enqueue({'tool_name': 'Edit', 'tool_input': {
                'file_path': '/tmp/x.py', 'old_string': 'a = 1', 'new_string': 'a = 2'}})
            wait_mode(page, 'approval'); page.wait_for_timeout(400)
            check('diff 红绿行渲染',
                  page.locator('.dl-del').count() == 1 and page.locator('.dl-add').count() == 1)
            page.keyboard.press('a'); page.wait_for_timeout(600)
            (resp_dir / f'{eid}.json').unlink(missing_ok=True)
            wait_mode(page, 'sliver')

            # usage 条（伪造缓存→桥 10s 节流，直接窗口期内断言渲染逻辑：手动注入 state 不可行，
            # 改走真实缓存文件路径——沙箱桥读全局 /tmp/island_rl.json，写后等节流窗）
            (Path(tmp) / 'rl.json').write_text(json.dumps({
                'five_hour': {'used_percentage': 35.0}, 'seven_day': {'used_percentage': 82.0}}))
            page.wait_for_timeout(11000)   # 桥 usage 缓存 10s 节流
            page.hover('#island'); wait_mode(page, 'compact')
            page.click('#island'); wait_mode(page, 'expanded')
            page.wait_for_timeout(800)
            check('分组头用量条(≥2)', page.locator('.sec-head .u-item').count() >= 2)
            check('7d 超80% 告警色', page.locator('.u-item.warn').count() >= 1)

            # 勿扰：mute 后 notify 不弹
            urllib.request.urlopen(urllib.request.Request(
                f'{BASE}/api/mute', data=b'{"muted": true}', method='POST'))
            page.keyboard.press('Escape'); wait_mode(page, 'sliver')
            enqueue({'id': f'notify_{time.time_ns()}', 'type': 'notify',
                     'hook_event_name': 'stop'})
            page.wait_for_timeout(1500)
            check('勿扰：通知不弹岛', page.evaluate('window.__island.mode') == 'sliver')
            urllib.request.urlopen(urllib.request.Request(
                f'{BASE}/api/mute', data=b'{"muted": false}', method='POST'))

            print('— T9 曜石玻璃/动效回归—')
            isl_shadow = page.evaluate(
                "getComputedStyle(document.getElementById('island')).boxShadow")
            check('去环化：岛体无均匀内描边环', 'inset' not in isl_shadow, isl_shadow[:80])
            check('rim-top 方向光存在且带 mask', page.evaluate(
                "!!document.querySelector('#island > .rim-top') && "
                "(getComputedStyle(document.querySelector('.rim-top')).webkitMaskImage"
                " || getComputedStyle(document.querySelector('.rim-top')).maskImage) !== 'none'"))
            check('ap-glow 非审批态隐藏', page.evaluate(
                "getComputedStyle(document.querySelector('.ap-glow')).display") == 'none')
            spring = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--spring-settle')")
            sup = page.evaluate("CSS.supports('animation-timing-function','linear(0, 1)')")
            check('真弹簧曲线注入(或环境不支持时兜底)',
                  (not sup) or spring.strip().startswith('linear('), spring[:30])
            page.evaluate(
                "(()=>{const b=document.createElement('button');b.className='btn';b.id='jp';"
                "b.style.cssText='position:fixed;left:5px;top:5px;width:60px;height:24px;"
                "z-index:99';document.body.appendChild(b)})()")
            page.mouse.move(35, 17)
            page.mouse.down(); page.wait_for_timeout(120)
            pressed = page.evaluate("document.getElementById('jp').style.transform")
            page.mouse.up(); page.mouse.move(300, 300)
            page.wait_for_timeout(1300)
            settled = page.evaluate("document.getElementById('jp').style.transform")
            check('果冻：按下压扁', 'scale' in pressed, pressed)
            check('果冻：静止清空 inline（零帧待机）', settled == '')
            page.evaluate("document.getElementById('jp').remove()")

            print('— T8 桥离线显示 —')
            bridge.kill(); bridge.wait()
            page.wait_for_timeout(1200)
            page.hover('#island')
            page.wait_for_timeout(400)
            check('离线样式生效', page.evaluate("document.getElementById('stage').classList.contains('offline')"))

            check('无 JS 错误', not errors, '; '.join(errors[:3]))
            browser.close()
    finally:
        if bridge.poll() is None:
            bridge.kill()
            bridge.wait()

    print(f'\n结果: {len(passed)} 通过, {len(failed)} 失败')
    if failed:
        print('失败项: ' + ', '.join(failed))
        sys.exit(1)


if __name__ == '__main__':
    main()
