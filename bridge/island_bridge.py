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
  队列   ~/.agents-island/queue.jsonl   hook 追加，35s 超时回落
  响应   ~/.agents-island/responses/<id>.json  {"decision":"allow"|"deny"}
  Always ~/.agents-island/always_<agent>（Stop hook 清除）

启动：bash apps/agents-island/launch/start_bridge.sh
"""
import argparse
import json
import logging
import subprocess
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
# 状态目录：~/.agents-island（持久化，WSL 重启不丢；/tmp 时代曾因重启全清空）
STATE_DIR        = Path(os.environ.get('ISLAND_STATE_DIR', str(Path.home() / '.agents-island')))
STATE_DIR.mkdir(exist_ok=True)
QUEUE_FILE       = Path(os.environ.get('ISLAND_QUEUE_FILE', str(STATE_DIR / 'queue.jsonl')))
RESP_DIR         = Path(os.environ.get('ISLAND_RESP_DIR', str(STATE_DIR / 'responses')))
ALWAYS_FLAGS     = {
    'claude': Path(os.environ.get('ISLAND_ALWAYS_CLAUDE', str(STATE_DIR / 'always_claude'))),
    'codex':  Path(os.environ.get('ISLAND_ALWAYS_CODEX', str(STATE_DIR / 'always_codex'))),
    'kimi':   Path(os.environ.get('ISLAND_ALWAYS_KIMI', str(STATE_DIR / 'always_kimi'))),
}
PENDING_TTL    = 40   # hook 35s 放弃，40s 后条目过期
ASK_TTL        = 125  # AskUserQuestion：hook 给 120s 作答窗口
NOTIFY_TTL     = 45   # 通知在岛上的存活秒数
SESSION_PERIOD = 8.0  # 会话全量扫描周期（有 UI 客户端在看时）
SESSION_IDLE   = 60.0 # 无客户端时的扫描周期（岛关闭 → 几乎零开销）
QUEUE_PERIOD   = 0.5  # 队列尾随间隔（审批延迟敏感，保持高频）
ORPHAN_AGE     = 60   # 孤儿响应文件清扫阈值
QUEUE_REPLAY_WINDOW = 40  # 桥启动回放窗口：仅补最近这么多秒内仍可能在等的审批（≈hook 35s 等待）
RL_CACHE       = Path(os.environ.get('ISLAND_RL_CACHE', str(STATE_DIR / 'rl.json')))  # statusline 包装写入的官方 rate_limits
# settings：env > 仓库旧位（向后兼容）> 状态目录（frozen 打包态唯一可写处）
_LEGACY_SETTINGS = Path(__file__).with_name('island_settings.json')
SETTINGS_FILE  = Path(os.environ.get('ISLAND_SETTINGS_FILE',
                 str(_LEGACY_SETTINGS if _LEGACY_SETTINGS.exists()
                     else STATE_DIR / 'settings.json')))  # muted/quiet_hours/auto_allow_timeout/remotes
import tempfile
LOG_FILE = os.environ.get('ISLAND_BRIDGE_LOG',
                          os.path.join(tempfile.gettempdir(), 'island_bridge.log'))

sys.path.insert(0, str(AGENTMONITOR_DIR))
import claude_monitor   # noqa: E402
import codex_monitor    # noqa: E402
import gemini_monitor   # noqa: E402
import kimi_monitor     # noqa: E402
import agy_monitor      # noqa: E402

# 会话扫描适配器注册表：接新 CLI 只需 vendor 加 <name>_monitor.py + 在此登记。
# 约定接口：callable() → [session]，session 至少含
#   session_id/slug/project/cwd/status/last_tool/age_seconds/runtime/is_live/source
# UI 端按 state.sessions 的键数据驱动渲染分组，无需改前端即可点亮新分组。
SESSION_ADAPTERS = {
    'claude': lambda: claude_monitor.get_all_sessions(),
    'codex':  lambda: codex_monitor.get_codex_sessions().get('sessions', []),
    'agy':    lambda: agy_monitor.get_agy_sessions().get('sessions', []),
    'gemini': lambda: gemini_monitor.get_gemini_sessions().get('sessions', []),
    'kimi':   lambda: kimi_monitor.get_kimi_sessions().get('sessions', []),
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
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
        self.muted     = False         # 勿扰：声效静音+通知不弹岛
        self.yolo_sessions = set()     # 会话级 YOLO：该 session 的工具审批秒放行
        # SSH 远程聚合：remote_poller 周期拉取各远程桥 /api/state 存这里，
        # snapshot 时合并视图；决策按 _remote 标转发，本地决不代写响应文件
        self.remote_data = {}          # name -> {url, ssh, sessions, pending, notify, ok}
        self.remote_notify_seen = set()
        self._settings = self._load_settings()
        self._usage    = {}
        self._usage_ts = 0.0

    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}

    def in_quiet_hours(self) -> bool:
        qh = self._settings.get('quiet_hours')   # 如 "22:00-08:00"
        if not qh:
            return False
        try:
            a, b = qh.split('-')
            now = time.strftime('%H:%M')
            return (a <= now < b) if a <= b else (now >= a or now < b)
        except ValueError:
            return False

    def usage(self) -> dict:
        if time.time() - self._usage_ts > 10:
            self._usage_ts = time.time()
            u = {}
            try:
                u = json.loads(RL_CACHE.read_text())
            except (OSError, json.JSONDecodeError):
                pass
            cx = _codex_usage()
            if cx:
                u['codex'] = cx
            self._usage = u
        return self._usage

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
                flag = always_flag_path(str(entry.get('agent_source') or 'claude').lower())
                if flag.exists():
                    write_response(eid, 'allow')
                    logger.info(f'auto-allow(always) {eid}')
                    return
                if entry.get('tool_name') == 'AskUserQuestion':
                    entry['kind'] = 'ask'   # 岛上作答：渲染选项按钮
                elif entry.get('tool_name') == 'ExitPlanMode':
                    entry['kind'] = 'plan'  # Plan 审阅：渲染 Markdown + 批准/驳回
                # 会话级 YOLO（展开面板 ⚡ 开关）：秒放行；ask/plan 仍上岛
                if (entry.get('session_id') in self.yolo_sessions
                        and entry.get('kind') not in ('ask', 'plan')):
                    write_response(eid, 'allow')
                    self.decisions += 1
                    logger.info(f'auto-allow(yolo) {eid} [{entry.get("tool_name")}]')
                    return
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
        auto = self.auto_allow_timeout()
        with self.lock:
            # 超时自动放行（Owner 可选模式，默认关）：普通工具审批倒计时到点
            # 无人操作 → 默认 allow，免得会话干等。仅当岛页面活着（最近 5s
            # 在拉状态，倒计时真的展示过）才放行；ask/plan 永不自动批
            # （自动答题/自动批计划风险不可接受，回落各自原超时路径）。
            if auto > 0 and now - self.last_client < 5:
                for eid in [k for k, v in self.pending.items()
                            if v.get('kind') not in ('ask', 'plan')
                            and now - v['_arrived'] > auto]:
                    entry = self.pending.pop(eid)
                    write_response(eid, 'allow')
                    self.decisions += 1
                    logger.info(f'auto-allow(timeout {auto}s) {eid} [{entry.get("tool_name")}]')
            for eid in [k for k, v in self.pending.items()
                        if now - v['_arrived'] > (ASK_TTL if v.get('kind') in ('ask', 'plan') else PENDING_TTL)]:
                self.pending.pop(eid, None)
                logger.info(f'expired {eid}')
            while self.notify and now - self.notify[0]['_arrived'] > NOTIFY_TTL:
                self.notify.popleft()

    def auto_allow_timeout(self) -> int:
        """超时自动放行秒数；0 = 关闭（默认）。"""
        try:
            return max(0, int(self._settings.get('auto_allow_timeout', 0)))
        except (TypeError, ValueError):
            return 0

    def remotes(self) -> list:
        """远程桥列表：[{name, url, ssh?}]。url 一般是 ssh -L 隧道的本地端口。"""
        r = self._settings.get('remotes')
        return r if isinstance(r, list) else []

    def merged_pending(self) -> list:
        """本地 + 远程 pending 合并视图（远程条目带 _remote/_remote_url）。"""
        items = list(self.pending.values())
        for name, d in self.remote_data.items():
            if not d.get('ok'):
                continue
            for e in d.get('pending', []):
                e2 = dict(e)
                e2['_remote'] = name
                e2['_remote_url'] = d.get('url', '')
                items.append(e2)
        return sorted(items, key=lambda e: e.get('_arrived', 0))

    def update_settings(self, patch: dict):
        with self.lock:
            self._settings.update(patch)
            try:
                SETTINGS_FILE.write_text(
                    json.dumps(self._settings, ensure_ascii=False, indent=2),
                    encoding='utf-8')
            except OSError as e:
                logger.warning(f'settings save: {e}')

    def snapshot(self) -> dict:
        self.last_client = time.time()
        with self.lock:
            sessions = {k: list(v) for k, v in self.sessions.items()}
            notify = list(self.notify)
            for name, d in self.remote_data.items():
                if not d.get('ok'):
                    continue
                for agent, sess_list in (d.get('sessions') or {}).items():
                    bucket = sessions.setdefault(agent, [])
                    for sess in sess_list:
                        s2 = dict(sess)
                        s2['remote'] = name
                        s2['remote_ssh'] = d.get('ssh', '')
                        bucket.append(s2)
                for n in d.get('notify', []):
                    n2 = dict(n)
                    n2['id'] = f"{name}:{n.get('id')}"
                    n2['_remote'] = name
                    notify.append(n2)
            return {
                'pending':  self.merged_pending(),
                'notify':   notify,
                'sessions': sessions,
                'remotes':  [{'name': name, 'ok': bool(d.get('ok'))}
                             for name, d in self.remote_data.items()],
                'stats':    {'decisions': self.decisions, 'uptime': int(time.time() - self.started)},
                'ui':       {'cursor_inside': self.ui_cursor,
                             'events': list(self.ui_events)},
                'usage':    self.usage(),
                'muted':    self.muted or self.in_quiet_hours(),
                'auto_allow_timeout': self.auto_allow_timeout(),
                'yolo_sessions': sorted(self.yolo_sessions),
                'lang': self._settings.get('lang', ''),
                'ts':       time.time(),
            }


STATE = BridgeState()
DEBUG_MODE = False


def _atomic_write_json(path, payload: dict):
    """临时文件 + os.replace 原子落盘。hook 以「文件存在」为就绪信号轮询，
    直接 write_text 会暴露空文件窗口 → hook 读到半截 JSON 兜底 allow（deny 被反转）。"""
    tmp = path.with_name(f'.{path.name}.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def write_response(perm_id: str, decision: str, reason: str = ''):
    """写响应文件（hook 读后即删；先应者赢（first-responder-wins））。
    reason: 岛上作答通道 —— deny+reason 把用户的选择/输入传回模型。"""
    RESP_DIR.mkdir(parents=True, exist_ok=True)
    payload = {'decision': decision}
    if reason:
        payload['reason'] = reason
    _atomic_write_json(RESP_DIR / f'{perm_id}.json', payload)


def always_flag_path(agent: str):
    """任意 agent 的 Always 标志路径：已知三家走 env 可覆盖表，
    其余（claude-fork 分支 CLI 等）按 STATE_DIR/always_<agent> 公式。"""
    return ALWAYS_FLAGS.get(agent, STATE_DIR / f'always_{agent}')


def write_always_flag(entry: dict):
    """镜像 popup._write_always_allow_flag 的载荷格式。"""
    agent = str(entry.get('agent_source') or 'claude').lower()
    flag  = always_flag_path(agent)
    _atomic_write_json(flag, {
        'agent_source': agent,
        'session_id':   entry.get('session_id') or '',
        'session_slug': entry.get('session_slug') or '',
        'project':      entry.get('project') or '',
        'created_at':   int(time.time()),
    })


def _tmux_locate(cwd: str) -> dict:
    """在 tmux 全部 pane 中按工作目录定位会话并切过去。无 tmux/未命中 → tmux:False。
    平台注记：Windows Terminal 无 tab 枚举/外部聚焦指定 tab 的 API（wt focus-tab
    只认 index 且无法查询会话在哪个 tab），故 Windows 侧精度上限=窗口级标题匹配；
    tmux 用户可获得窗口内 pane 级精确跳转。"""
    if not cwd:
        return {'tmux': False}
    try:
        out = subprocess.run(
            ['tmux', 'list-panes', '-a', '-F',
             '#{session_name}\t#{window_index}\t#{pane_id}\t#{pane_current_path}'],
            capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return {'tmux': False}
        for line in out.stdout.splitlines():
            try:
                sess, widx, pane, path = line.split('\t')
            except ValueError:
                continue
            if path.rstrip('/') == cwd.rstrip('/'):
                subprocess.run(['tmux', 'select-window', '-t', f'{sess}:{widx}'],
                               capture_output=True, timeout=3)
                subprocess.run(['tmux', 'select-pane', '-t', pane],
                               capture_output=True, timeout=3)
                subprocess.run(['tmux', 'switch-client', '-t', f'{sess}:{widx}'],
                               capture_output=True, timeout=3)
                logger.info(f'jump_assist: tmux -> {sess}:{widx} {pane}')
                return {'tmux': True, 'session_name': sess}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {'tmux': False}


def _codex_usage() -> dict:
    """Codex 用量：最新 rollout 的最后一条 token_count.rate_limits（官方数据）。"""
    try:
        base = Path.home() / '.codex' / 'sessions'
        latest = max(base.glob('*/*/*/rollout-*.jsonl'), key=lambda f: f.stat().st_mtime,
                     default=None)
        if not latest:
            return {}
        with open(latest, 'rb') as f:
            f.seek(max(0, latest.stat().st_size - 65536))
            tail = f.read().decode('utf-8', errors='replace')
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rl = (d.get('payload') or d).get('rate_limits') or {}
            out = {}
            pri, sec = rl.get('primary'), rl.get('secondary')
            if pri and pri.get('used_percent') is not None:
                out['five_hour'] = {'used_percentage': pri['used_percent']}
            if sec and sec.get('used_percent') is not None:
                out['seven_day'] = {'used_percentage': sec['used_percent']}
            return out
    except Exception:
        pass
    return {}


def _claude_session_extras(sess: dict):
    """subagent 标记 + idle recap：读 transcript 头/尾少量字节。"""
    try:
        fp = Path(sess.get('file') or '')
        if not fp.exists():
            return
        with open(fp, 'rb') as f:
            head = f.read(4096).decode('utf-8', errors='replace')
            size = fp.stat().st_size
            f.seek(max(0, size - 16384))
            tail = f.read().decode('utf-8', errors='replace')
        if '"isSidechain":true' in head or '"isSidechain": true' in head:
            sess['subagent'] = True
        # recap：最后一条 assistant 文本（截 90 字）
        for line in reversed(tail.splitlines()):
            if '"type":"assistant"' not in line and '"type": "assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (d.get('message') or {}).get('content') or []
            for blk in content:
                if isinstance(blk, dict) and blk.get('type') == 'text' and blk.get('text'):
                    sess['recap'] = blk['text'].strip().replace('\n', ' ')[:90]
                    return
    except Exception:
        pass


def cleanup_orphan_responses():
    """清扫迟到方写下的孤儿响应文件（first-responder-wins 副产品）。"""
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

def _entry_epoch(entry: dict) -> float:
    """从 id 尾部 _<epoch> 解析入队时刻；解析不出返回 0（视为过旧、不回放）。"""
    try:
        return float(str(entry.get('id', '')).rsplit('_', 1)[-1])
    except (ValueError, IndexError):
        return 0.0


def replay_inflight_queue(now: float) -> int:
    """桥启动/重启时回放队列尾部仍在途的审批，返回后续增量尾随的起点 offset。
    痛点：桥重启瞬间入队的工具调用（hook 已阻塞等审批）会被"跳到末尾"永久漏掉，
    hook 干等到超时回落原生提示——文件写这类不在白名单的工具首当其冲。
    只补 ①最近 QUEUE_REPLAY_WINDOW 秒内（hook 还可能在等）②尚无响应文件
    （hook 未被独立答复）③未 seen 过 的条目，其余跳过（防重启风暴/幽灵卡）。"""
    if not QUEUE_FILE.exists():
        return 0
    st = QUEUE_FILE.stat()
    try:
        with open(QUEUE_FILE, errors='replace') as f:
            f.seek(max(0, st.st_size - 65536))
            tail = f.read()
    except OSError:
        return st.st_size
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        eid = str(entry.get('id') or '')
        if not eid or eid in STATE.seen_ids:
            continue
        if now - _entry_epoch(entry) > QUEUE_REPLAY_WINDOW:
            continue
        if (RESP_DIR / f'{eid}.json').exists():
            continue      # 已有响应待 hook 自取，勿重复上岛
        STATE.add_entry(entry)
        logger.info(f'replay in-flight {eid} [{entry.get("tool_name")}]')
    return st.st_size


def queue_tailer():
    """尾随队列文件：按 (inode, offset) 增量读取，截断/轮转自动复位。
    启动时回放最近仍在途的审批（见 replay_inflight_queue），其余历史不回放（防重启风暴）。"""
    ino, offset = -1, 0
    if QUEUE_FILE.exists():
        st = QUEUE_FILE.stat()
        ino = st.st_ino
        offset = replay_inflight_queue(time.time())
    last_orphan_sweep = time.time()
    while True:
        try:
            if time.time() - last_orphan_sweep > 300:   # 孤儿响应文件周期清扫（first-responder-wins 竞态副产品）
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


def remote_poller():
    """周期拉取各远程桥 /api/state（经 ssh -L 隧道的本地端口），失败即标记
    offline 并丢弃其 pending（防幽灵审批卡死岛）。无远程配置时线程空转极慢。"""
    import urllib.request
    while True:
        remotes = STATE.remotes()
        for r in remotes:
            name, url = str(r.get('name') or ''), str(r.get('url') or '')
            if not name or not url:
                continue
            try:
                with urllib.request.urlopen(f'{url}/api/state', timeout=2) as resp:
                    d = json.loads(resp.read())
                with STATE.lock:
                    STATE.remote_data[name] = {
                        'url': url, 'ssh': str(r.get('ssh') or ''),
                        'sessions': d.get('sessions') or {},
                        'pending': d.get('pending') or [],
                        'notify': d.get('notify') or [],
                        'ok': True, 'ts': time.time(),
                    }
            except Exception as e:
                with STATE.lock:
                    prev = STATE.remote_data.get(name) or {}
                    if prev.get('ok'):
                        logger.warning(f'remote {name} offline: {type(e).__name__}')
                    STATE.remote_data[name] = {**prev, 'url': url,
                                               'ok': False, 'pending': []}
        # 清理已被移除的远程配置
        names = {str(r.get('name') or '') for r in remotes}
        with STATE.lock:
            for stale in [k for k in STATE.remote_data if k not in names]:
                STATE.remote_data.pop(stale, None)
        time.sleep(3 if remotes else 5)   # 空配置也保持低频心跳（运行中可挂载）


def forward_remote_decision(entry: dict, decision: str, reason: str) -> bool:
    """把岛上决策转发给条目所属远程桥。"""
    import urllib.request
    url = entry.get('_remote_url')
    if not url:
        return False
    try:
        body = json.dumps({'id': entry.get('id'), 'decision': decision,
                           'reason': reason}).encode()
        req = urllib.request.Request(f'{url}/api/decision', data=body,
                                     method='POST',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = bool(json.loads(resp.read()).get('ok'))
        # 本地缓存里立刻摘掉，避免下一轮拉取前重复显示
        with STATE.lock:
            d = STATE.remote_data.get(entry.get('_remote') or '')
            if d:
                d['pending'] = [p for p in d.get('pending', [])
                                if p.get('id') != entry.get('id')]
        logger.info(f'decision {entry.get("id")}: {decision} -> remote {entry.get("_remote")}')
        return ok
    except Exception as e:
        logger.warning(f'forward decision failed: {type(e).__name__}: {e}')
        return False


def session_scanner():
    """周期聚合各 agent 适配器的会话（每个独立容错）。"""
    scanners = SESSION_ADAPTERS
    while True:
        result = {}
        for name, fn in scanners.items():
            try:
                result[name] = fn() or []
            except Exception as e:
                logger.warning(f'scan {name}: {e}')
                result[name] = STATE.sessions.get(name, [])
        for sess in result.get('claude', []):
            if sess.get('is_live'):
                _claude_session_extras(sess)
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
            reason = str(data.get('reason') or '')[:2000]
            if entry is None:
                # 远程条目：转发给所属远程桥
                with STATE.lock:
                    remote_entry = next((e for e in STATE.merged_pending()
                                         if e.get('id') == eid and e.get('_remote')), None)
                if remote_entry:
                    ok = forward_remote_decision(
                        remote_entry, 'deny' if decision == 'deny' else decision, reason)
                    return self._json({'ok': ok, 'remote': remote_entry.get('_remote')})
                return self._json({'ok': False, 'reason': 'unknown_or_expired'}, 410)
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
                items = STATE.merged_pending()
                entry = items[0] if items else None
                if entry and not entry.get('_remote'):
                    STATE.pending.pop(entry['id'], None)
                if entry:
                    STATE.decisions += 1
            if not entry:
                return self._json({'ok': False, 'reason': 'no_pending'})
            if entry.get('_remote'):
                ok = forward_remote_decision(entry, action, '')
                logger.info(f'hotkey {action} -> remote {entry.get("_remote")}:{entry["id"]}')
                return self._json({'ok': ok, 'id': entry['id'],
                                   'remote': entry.get('_remote')})
            if action == 'always':
                write_always_flag(entry)
            write_response(entry['id'], 'deny' if action == 'deny' else 'allow')
            logger.info(f'hotkey {action} -> {entry["id"]}')
            return self._json({'ok': True, 'id': entry['id']})

        if self.path == '/api/mute':
            with STATE.lock:
                STATE.muted = bool(data.get('muted')) if 'muted' in data else not STATE.muted
            logger.info(f'muted -> {STATE.muted}')
            return self._json({'ok': True, 'muted': STATE.muted})

        if self.path == '/api/jump_assist':
            # 跳转辅助：WSL 侧 tmux 精确定位（按 cwd 匹配 pane →
            # select-window/pane + switch-client），岛壳再聚焦宿主终端窗口。
            return self._json(_tmux_locate(str(data.get('cwd') or '')))

        if self.path == '/api/session_yolo':
            sid = str(data.get('session_id') or '')
            if sid:
                with STATE.lock:
                    if data.get('on'):
                        STATE.yolo_sessions.add(sid)
                    else:
                        STATE.yolo_sessions.discard(sid)
                logger.info(f'yolo {"on" if data.get("on") else "off"} {sid[:12]}')
            return self._json({'ok': True, 'yolo_sessions': sorted(STATE.yolo_sessions)})

        if self.path == '/api/settings':
            # 白名单字段；auto_allow_timeout 传 0 关闭 / 秒数开启（toggle 由调用方做）
            patch = {k: data[k] for k in ('auto_allow_timeout', 'quiet_hours', 'lang', 'remotes')
                     if k in data}
            if patch:
                STATE.update_settings(patch)
                logger.info(f'settings -> {patch}')
            return self._json({'ok': True, 'settings': {
                'auto_allow_timeout': STATE.auto_allow_timeout()}})

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
                import tempfile as _tf
                with open(os.path.join(_tf.gettempdir(), 'island_client.log'), 'a') as f:
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

    threading.Thread(target=remote_poller, daemon=True).start()

    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    logger.info(f'island_bridge up on 127.0.0.1:{args.port} debug={DEBUG_MODE}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
