#!/usr/bin/env python3
"""
Agents Island — WSL 侧数据桥
============================
只读复用 AgentMonitor 的会话扫描模块，尾随审批队列，向 Windows 侧岛 UI
提供 HTTP 接口（127.0.0.1:5599），并代写审批响应文件。

铁律：本进程绝不修改 AgentMonitor/ 内任何文件，仅 import 与读取。

接口：
  GET  /                 → 岛 UI 静态页（web/island.html）
  GET  /api/state        → {pending, notify, sessions, ts}
  POST /api/decision     → {id, decision: allow|deny|always}
  GET  /api/health       → {ok, uptime}
  POST /api/test/enqueue → 仅 --debug 时开放，注入伪审批条目（写真实队列文件）

协议（与 AgentMonitor hooks 完全一致）：
  队列   /tmp/claude_perm_queue.jsonl   hook 追加，35s 超时默认 allow
  响应   /tmp/claude_perm_responses/<id>.json  {"decision":"allow"|"deny"}
  Always /tmp/claude_always_allow | /tmp/codex_always_allow（Stop hook 清除）

启动：bash apps/agents-island/launch/start_bridge.sh
"""
import argparse
import json
import logging
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── 路径与常量 ────────────────────────────────────────────────────────
import os

# 会话扫描模块来源：默认用随包 vendor 副本（分发自包含）；
# 设 ISLAND_AGENTMONITOR_DIR 可改用本机 agent-monitor 检出（跟上游最新）
AGENTMONITOR_DIR = Path(os.environ.get('ISLAND_AGENTMONITOR_DIR',
                                       str(Path(__file__).resolve().parent / 'vendor')))
WEB_DIR          = Path(__file__).resolve().parent.parent / 'web'
# 路径可由环境变量覆盖——pytest 在隔离沙箱跑，不碰真实队列（防夜间弹窗风暴）
QUEUE_FILE       = Path(os.environ.get('ISLAND_QUEUE_FILE', '/tmp/claude_perm_queue.jsonl'))
RESP_DIR         = Path(os.environ.get('ISLAND_RESP_DIR', '/tmp/claude_perm_responses'))
ALWAYS_FLAGS     = {
    'claude': Path(os.environ.get('ISLAND_ALWAYS_CLAUDE', '/tmp/claude_always_allow')),
    'codex':  Path(os.environ.get('ISLAND_ALWAYS_CODEX', '/tmp/codex_always_allow')),
}
PENDING_TTL    = 40   # hook 35s 放弃，40s 后条目过期
ASK_TTL        = 125  # AskUserQuestion：hook 给 120s 作答窗口
NOTIFY_TTL     = 45   # 通知在岛上的存活秒数
SESSION_PERIOD = 8.0  # 会话全量扫描周期（有 UI 客户端在看时）
SESSION_IDLE   = 60.0 # 无客户端时的扫描周期（岛关闭 → 几乎零开销）
QUEUE_PERIOD   = 0.5  # 队列尾随间隔（审批延迟敏感，保持高频）
ORPHAN_AGE     = 60   # 孤儿响应文件清扫阈值

sys.path.insert(0, str(AGENTMONITOR_DIR))
import claude_monitor   # noqa: E402
import codex_monitor    # noqa: E402
import gemini_monitor   # noqa: E402
import kimi_monitor     # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('/tmp/island_bridge.log'), logging.StreamHandler()],
)
logger = logging.getLogger('island_bridge')


class BridgeState:
    """线程安全的桥状态：待审批、通知、会话缓存。"""

    def __init__(self):
        self.lock      = threading.Lock()
        self.pending   = {}            # id → entry(+_arrived)
        self.notify    = deque(maxlen=20)
        self.sessions  = {}            # agent → [session]
        self.seen_ids  = set()
        self.started   = time.time()
        self.decisions = 0             # 统计：岛已处理审批数
        self.last_client = 0.0         # 最近一次 /api/state 拉取时间（驱动扫描降频）
        # Python→JS 事件中转（evaluate_js 会被 pywebview 串行锁堵死，岛壳
        # 一律 POST 到这里、页面随 /api/state 轮询取走 —— 无管道可堵）
        self.ui_cursor = False         # 全局光标是否在岛窗口内
        self.ui_seq    = 0
        self.ui_events = deque(maxlen=20)   # [{seq, action}]

    # ── 队列条目 ──
    def add_entry(self, entry: dict):
        eid = str(entry.get('id') or '')
        if not eid or eid in self.seen_ids:
            return
        with self.lock:
            if len(self.seen_ids) > 5000:
                self.seen_ids.clear()
            self.seen_ids.add(eid)
            entry['_arrived'] = time.time()
            self._enrich(entry)
            if entry.get('type') == 'notify':
                self.notify.append(entry)
                logger.info(f'notify {eid} [{entry.get("agent_source")}] {entry.get("hook_event_name")}')
            else:
                # Always 标志生效中 → 镜像 popup 行为：立即放行，不上岛
                flag = ALWAYS_FLAGS.get(entry.get('agent_source', 'claude'), ALWAYS_FLAGS['claude'])
                if flag.exists():
                    write_response(eid, 'allow')
                    logger.info(f'auto-allow(always) {eid}')
                    return
                if entry.get('tool_name') == 'AskUserQuestion':
                    entry['kind'] = 'ask'   # 岛上作答：渲染选项按钮
                self.pending[eid] = entry
                logger.info(f'pending {eid} [{entry.get("agent_source")}] {entry.get("tool_name")}')

    def _enrich(self, entry: dict):
        """补全 agent_source / project（镜像 monitor.py._enrich_perm_data）。"""
        sid = entry.get('session_id')
        if sid:
            for source, sess_list in self.sessions.items():
                for s in sess_list:
                    if s.get('session_id') == sid:
                        entry['session_slug'] = s.get('slug')
                        entry['project']      = s.get('project')
                        entry['title']        = entry.get('title') or s.get('title')
                        entry.setdefault('agent_source', source)
                        return
        if entry.get('hook_event_name') == 'BeforeTool':
            entry['agent_source'] = 'gemini'
        entry.setdefault('agent_source', 'claude')

    def expire(self):
        now = time.time()
        with self.lock:
            for eid in [k for k, v in self.pending.items()
                        if now - v['_arrived'] > (ASK_TTL if v.get('kind') == 'ask' else PENDING_TTL)]:
                self.pending.pop(eid, None)
                logger.info(f'expired {eid}')
            while self.notify and now - self.notify[0]['_arrived'] > NOTIFY_TTL:
                self.notify.popleft()

    def snapshot(self) -> dict:
        self.last_client = time.time()
        with self.lock:
            return {
                'pending':  sorted(self.pending.values(), key=lambda e: e['_arrived']),
                'notify':   list(self.notify),
                'sessions': self.sessions,
                'stats':    {'decisions': self.decisions, 'uptime': int(time.time() - self.started)},
                'ui':       {'cursor_inside': self.ui_cursor,
                             'events': list(self.ui_events)},
                'ts':       time.time(),
            }


STATE = BridgeState()
DEBUG_MODE = False


def write_response(perm_id: str, decision: str, reason: str = ''):
    """写响应文件（hook 读后即删；先应者赢，见 D-117）。
    reason: 岛上作答通道 —— deny+reason 把用户的选择/输入传回模型。"""
    RESP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {'decision': decision}
    if reason:
        payload['reason'] = reason
    (RESP_DIR / f'{perm_id}.json').write_text(
        json.dumps(payload, ensure_ascii=False))


def write_always_flag(entry: dict):
    """镜像 popup._write_always_allow_flag 的载荷格式。"""
    agent = str(entry.get('agent_source') or 'claude').lower()
    flag  = ALWAYS_FLAGS.get(agent, ALWAYS_FLAGS['claude'])
    flag.write_text(json.dumps({
        'agent_source': agent,
        'session_id':   entry.get('session_id') or '',
        'session_slug': entry.get('session_slug') or '',
        'project':      entry.get('project') or '',
        'created_at':   int(time.time()),
    }, ensure_ascii=False), encoding='utf-8')


def cleanup_orphan_responses():
    """清扫迟到方写下的孤儿响应文件（D-117）。"""
    if not RESP_DIR.exists():
        return
    now = time.time()
    for f in RESP_DIR.glob('*.json'):
        try:
            if now - f.stat().st_mtime > ORPHAN_AGE:
                f.unlink()
                logger.info(f'orphan cleaned: {f.name}')
        except OSError:
            pass


# ── 后台线程 ──────────────────────────────────────────────────────────

def queue_tailer():
    """尾随队列文件：按 (inode, offset) 增量读取，截断/轮转自动复位。
    启动时直接跳到文件末尾——历史条目不回放（防重启风暴）。"""
    ino, offset = -1, 0
    if QUEUE_FILE.exists():
        st = QUEUE_FILE.stat()
        ino, offset = st.st_ino, st.st_size
    last_orphan_sweep = time.time()
    while True:
        try:
            if time.time() - last_orphan_sweep > 300:   # 孤儿响应文件周期清扫（D-117 竞态副产品）
                cleanup_orphan_responses()
                last_orphan_sweep = time.time()
            if QUEUE_FILE.exists():
                st = QUEUE_FILE.stat()
                if st.st_ino != ino or st.st_size < offset:
                    ino, offset = st.st_ino, 0     # 轮转或截断 → 从头读
                if st.st_size > offset:
                    with open(QUEUE_FILE, errors='replace') as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            STATE.add_entry(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            else:
                ino, offset = -1, 0
            STATE.expire()
        except Exception as e:
            logger.warning(f'tailer: {e}')
        time.sleep(QUEUE_PERIOD)


def session_scanner():
    """每 3s 聚合四个 agent 的会话（每个独立容错）。"""
    scanners = {
        'claude': lambda: claude_monitor.get_all_sessions(),
        'codex':  lambda: codex_monitor.get_codex_sessions().get('sessions', []),
        'gemini': lambda: gemini_monitor.get_gemini_sessions().get('sessions', []),
        'kimi':   lambda: kimi_monitor.get_kimi_sessions().get('sessions', []),
    }
    while True:
        result = {}
        for name, fn in scanners.items():
            try:
                result[name] = fn() or []
            except Exception as e:
                logger.warning(f'scan {name}: {e}')
                result[name] = STATE.sessions.get(name, [])
        with STATE.lock:
            STATE.sessions = result
        # 自适应降频：岛在看 → 8s；没人看 → 60s（接近零开销待机）
        active = time.time() - STATE.last_client < 30
        time.sleep(SESSION_PERIOD if active else SESSION_IDLE)


# ── HTTP ──────────────────────────────────────────────────────────────

MIME = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
        '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 访问日志静默，避免刷盘

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/state':
            return self._json(STATE.snapshot())
        if path == '/api/health':
            age = time.time() - STATE.last_client if STATE.last_client else -1
            return self._json({'ok': True, 'uptime': int(time.time() - STATE.started),
                               'client_age': round(age, 1)})
        # 静态文件（岛 UI 同源直出，免 CORS / 跨系统路径问题）
        if path == '/':
            path = '/island.html'
        target = (WEB_DIR / path.lstrip('/')).resolve()
        if target.is_file() and WEB_DIR.resolve() in target.parents:
            self.send_response(200)
            self.send_header('Content-Type', MIME.get(target.suffix, 'application/octet-stream'))
            self.send_header('Cache-Control', 'no-store')
            body = target.read_bytes()
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({'error': 'not found'}, 404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            data = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self._json({'error': 'bad json'}, 400)

        if self.path == '/api/decision':
            eid      = str(data.get('id') or '')
            decision = data.get('decision')
            if decision not in ('allow', 'deny', 'always'):
                return self._json({'error': 'bad decision'}, 400)
            with STATE.lock:
                entry = STATE.pending.pop(eid, None)
                STATE.decisions += 1
            if entry is None:
                return self._json({'ok': False, 'reason': 'unknown_or_expired'}, 410)
            reason = str(data.get('reason') or '')[:2000]
            if decision == 'always':
                write_always_flag(entry)
            write_response(eid, 'deny' if decision == 'deny' else 'allow', reason)
            logger.info(f'decision {eid}: {decision}{" +reason" if reason else ""}')
            return self._json({'ok': True})

        if self.path == '/api/hotkey':
            action = data.get('action')
            if action not in ('allow', 'deny', 'always'):
                return self._json({'error': 'bad action'}, 400)
            with STATE.lock:
                items = sorted(STATE.pending.values(), key=lambda e: e['_arrived'])
                entry = items[0] if items else None
                if entry:
                    STATE.pending.pop(entry['id'], None)
                    STATE.decisions += 1
            if not entry:
                return self._json({'ok': False, 'reason': 'no_pending'})
            if action == 'always':
                write_always_flag(entry)
            write_response(entry['id'], 'deny' if action == 'deny' else 'allow')
            logger.info(f'hotkey {action} -> {entry["id"]}')
            return self._json({'ok': True, 'id': entry['id']})

        if self.path == '/api/ui_event':
            kind = data.get('type')
            with STATE.lock:
                if kind == 'cursor':
                    STATE.ui_cursor = bool(data.get('inside'))
                elif kind == 'action':
                    STATE.ui_seq += 1
                    STATE.ui_events.append({'seq': STATE.ui_seq,
                                            'action': str(data.get('action', ''))[:24]})
            return self._json({'ok': True})

        if self.path == '/api/client_log':
            # 岛页面黑匣子：UI 侧关键事件/JS 错误落盘，便于跨系统诊断
            try:
                with open('/tmp/island_client.log', 'a') as f:
                    f.write(f'{time.strftime("%H:%M:%S")} {data.get("msg", "")}\n')
            except OSError:
                pass
            return self._json({'ok': True})

        if self.path == '/api/test/enqueue' and DEBUG_MODE:
            # 写真实队列文件，走完整 tailer 链路（端到端自测用）
            entry = {
                'id': data.get('id') or f'test_{int(time.time()*1000)}',
                'session_id': data.get('session_id', 'test-session'),
                'tool_name': data.get('tool_name', 'Bash'),
                'tool_input': data.get('tool_input', {'command': 'echo island-test'}),
            }
            if data.get('type'):
                entry['type'] = data['type']
                entry['hook_event_name'] = data.get('hook_event_name', 'stop')
            if data.get('agent_source'):
                entry['agent_source'] = data['agent_source']
            with open(QUEUE_FILE, 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            return self._json({'ok': True, 'id': entry['id']})

        self._json({'error': 'not found'}, 404)


def main():
    global DEBUG_MODE
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5599)
    ap.add_argument('--debug', action='store_true', help='开放 /api/test/enqueue')
    args = ap.parse_args()
    DEBUG_MODE = args.debug

    cleanup_orphan_responses()
    threading.Thread(target=queue_tailer, daemon=True).start()
    threading.Thread(target=session_scanner, daemon=True).start()

    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    logger.info(f'island_bridge up on 127.0.0.1:{args.port} debug={DEBUG_MODE}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
