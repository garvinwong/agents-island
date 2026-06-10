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


def wait_mode(page, mode, timeout=6000):
    page.wait_for_function(f'window.__island.mode === "{mode}"', timeout=timeout)


def main():
    tmp = tempfile.mkdtemp(prefix='island_ui_')
    resp_dir = Path(tmp) / 'responses'
    env = dict(os.environ,
               ISLAND_QUEUE_FILE=str(Path(tmp) / 'queue.jsonl'),
               ISLAND_RESP_DIR=str(resp_dir),
               ISLAND_ALWAYS_CLAUDE=str(Path(tmp) / 'always_claude'),
               ISLAND_ALWAYS_CODEX=str(Path(tmp) / 'always_codex'))
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
            wait_mode(page, 'sliver', timeout=5000)
            check('2.5s 后自动缩回 sliver', True)

            print('— T12 多条排队 —')
            ids = [enqueue({'tool_name': f'Tool{i}', 'tool_input': {'command': f'cmd{i}'}})
                   for i in range(3)]
            wait_mode(page, 'approval')
            page.wait_for_timeout(600)
            check('队列徽数 1 / 3', page.locator('#ap-queue').text_content() == '1 / 3')
            page.keyboard.press('d')
            page.wait_for_timeout(600)
            check('Deny 响应',
                  json.loads((resp_dir / f'{ids[0]}.json').read_text())['decision'] == 'deny')
            page.keyboard.press('a')
            page.wait_for_timeout(600)
            page.keyboard.press('s')
            page.wait_for_timeout(700)
            check('Always 写标志', (Path(tmp) / 'always_claude').exists())
            check('Always 响应 allow',
                  json.loads((resp_dir / f'{ids[2]}.json').read_text())['decision'] == 'allow')
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
