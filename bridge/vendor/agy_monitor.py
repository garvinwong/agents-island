"""
agy_monitor.py — 监控运行中的 AGY（Antigravity CLI）进程 + 读取历史会话

数据源（~/.gemini/antigravity-cli/）：
- conversations/<uuid>.db        每会话一个 SQLite 轨迹库（WAL 模式，活跃会话持续写 -wal）
- conversations/<uuid>.db 内：
    trajectory_metadata_blob.data  protobuf，含明文 `file:///<workspace>` 工作区
    steps.step_payload             protobuf，工具调用名以 [len][ascii] 内嵌（如 run_command）
- history.jsonl                  {display, timestamp, workspace, conversationId?} 输入历史 → 标题映射

判定：
- 实时进程：ps aux 扫 exe basename ∈ {agy, antigravity}（或 antigravity-cli/bin 路径）
- working：进程树 CPU 活动 或 会话 db/-wal 最近 ACTIVE_WRITE_WINDOW 秒内有写入
"""
import json
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

AGY_DIR   = Path.home() / '.gemini' / 'antigravity-cli'
CONV_DIR  = AGY_DIR / 'conversations'
HIST_FILE = AGY_DIR / 'history.jsonl'

HISTORY_DAYS        = 7
ACTIVE_WRITE_WINDOW = 8

# db 元信息缓存：path → (mtime_key, meta)，避免每轮扫描重开 SQLite
_DB_META_CACHE: Dict[str, tuple] = {}


def get_agy_sessions() -> Dict[str, Any]:
    """扫描实时进程 + 读取历史，返回合并后的会话列表。"""
    live_sessions = _scan_live_processes()
    history       = _get_history()

    live_ids = {s['session_id'] for s in live_sessions}
    hist_filtered = [s for s in history if s['session_id'] not in live_ids]

    return {
        'total'   : len(live_sessions),
        'sessions': live_sessions + hist_filtered,
    }


# ── 实时进程扫描 ──────────────────────────────────────────────────────

def _scan_live_processes() -> List[Dict[str, Any]]:
    procs = []
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            p = _parse_agy_process(line)
            if p:
                procs.append(p)
    except Exception:
        pass
    if not procs:
        return []

    # 活跃会话 db 按写入时间倒序，与进程按 cwd==workspace 优先配对
    convs = _ranked_conversations()
    used: set = set()
    sessions = []
    for pid, cwd in procs:
        conv = None
        for c in convs:
            if c['conv_id'] in used:
                continue
            if cwd and c.get('workspace') == cwd:
                conv = c
                break
        if conv is None:
            for c in convs:
                if c['conv_id'] not in used:
                    conv = c
                    break
        if conv:
            used.add(conv['conv_id'])

        status = 'standby'
        if _has_tree_cpu_activity(pid, 0.15):
            status = 'executing_tool'
        elif conv and (time.time() - conv['mtime']) <= ACTIVE_WRITE_WINDOW:
            status = 'executing_tool'

        sessions.append({
            'session_id' : (conv or {}).get('conv_id') or f'agy-{pid}',
            'pid'        : pid,
            'slug'       : (conv or {}).get('slug') or f'agy-{pid}',
            'project'    : os.path.basename(cwd) if cwd else 'agy-project',
            'cwd'        : cwd or '未知目录',
            'status'     : status,
            'last_tool'  : (conv or {}).get('last_tool'),
            'age_seconds': _get_age_seconds(pid),
            'runtime'    : _get_proc_runtime(pid),
            'is_live'    : True,
            'source'     : 'agy',
        })
    return sessions


def _parse_agy_process(ps_line: str) -> tuple | None:
    """返回 (pid, cwd)；非 agy 进程返回 None。"""
    if 'grep' in ps_line or 'agy_monitor' in ps_line:
        return None
    parts = ps_line.split(None, 10)
    if len(parts) < 11:
        return None
    pid = parts[1]
    cmd = parts[10]
    exe = cmd.split()[0] if cmd.strip() else ''
    exe_base = os.path.basename(exe)
    if not (re.fullmatch(r'agy|antigravity', exe_base, re.IGNORECASE)
            or 'antigravity-cli/bin' in exe):
        return None
    return pid, _get_proc_cwd(pid)


# ── 会话 db 解析 ──────────────────────────────────────────────────────

def _ranked_conversations(max_age: float = 86400) -> List[Dict[str, Any]]:
    """conversations/*.db 按最近写入排序（含 -wal），只取 max_age 内的。"""
    out = []
    if not CONV_DIR.is_dir():
        return out
    now = time.time()
    title_map = _history_title_map()
    for db in CONV_DIR.glob('*.db'):
        try:
            mtime = db.stat().st_mtime
            wal = db.with_name(db.name + '-wal')
            if wal.exists():
                mtime = max(mtime, wal.stat().st_mtime)
            if now - mtime > max_age:
                continue
            meta = _db_meta(db, mtime)
            conv_id = db.stem
            out.append({
                'conv_id'  : conv_id,
                'mtime'    : mtime,
                'workspace': meta.get('workspace', ''),
                'last_tool': meta.get('last_tool'),
                'slug'     : title_map.get(conv_id)
                             or title_map.get(meta.get('workspace', ''))
                             or conv_id[:8],
            })
        except Exception:
            continue
    out.sort(key=lambda c: -c['mtime'])
    return out


def _db_meta(db: Path, mtime: float) -> Dict[str, Any]:
    """读取单个会话 db 的 workspace / last_tool（按 mtime 缓存）。"""
    key = str(db)
    cached = _DB_META_CACHE.get(key)
    if cached and cached[0] == int(mtime):
        return cached[1]

    meta: Dict[str, Any] = {}
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1.0)
        try:
            cur = con.cursor()
            row = cur.execute(
                'select data from trajectory_metadata_blob limit 1').fetchone()
            if row and row[0]:
                m = re.search(rb'file://(/[\x20-\x7e]+?)(?=[\x00-\x1f]|$)', row[0])
                if m:
                    meta['workspace'] = m.group(1).decode('utf-8', 'replace')
            # 倒序找最近一次工具调用名（protobuf: \n \x08 <8字符id> \x12 <len> <name>）
            for (payload,) in cur.execute(
                    'select step_payload from steps order by idx desc limit 12'):
                name = _extract_tool_name(payload)
                if name:
                    meta['last_tool'] = name
                    break
        finally:
            con.close()
    except Exception:
        pass

    _DB_META_CACHE[key] = (int(mtime), meta)
    if len(_DB_META_CACHE) > 64:
        _DB_META_CACHE.pop(next(iter(_DB_META_CACHE)))
    return meta


def _extract_tool_name(payload: Any) -> str | None:
    if not isinstance(payload, (bytes, bytearray)):
        return None
    last = None
    for m in re.finditer(rb'\n\x08[a-z0-9]{8}\x12([\x01-\x28])', payload):
        ln = m.group(1)[0]
        name = payload[m.end():m.end() + ln]
        if len(name) == ln and re.fullmatch(rb'[A-Za-z][A-Za-z0-9_\-]*', name):
            last = name.decode()
    return _normalize_agy_tool_name(last) if last else None


def _normalize_agy_tool_name(name: str) -> str:
    mapping = {
        'run_command'      : 'Bash',
        'write_to_file'    : 'Write',
        'replace_file_content': 'Patch',
        'view_file'        : 'Read',
        'read_file'        : 'Read',
        'list_dir'         : 'List',
        'grep_search'      : 'Search',
        'find_by_name'     : 'Search',
        'codebase_search'  : 'Search',
        'search_web'       : 'Web',
        'read_url_content' : 'Web',
        'browser_preview'  : 'Web',
    }
    return mapping.get(name) or name.replace('_', ' ')[:18]


def _history_title_map() -> Dict[str, str]:
    """history.jsonl → {conversationId|workspace: 首句输入}（首句优先，UUID 键优先级高）。"""
    out: Dict[str, str] = {}
    try:
        with open(HIST_FILE, errors='replace') as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                text = str(d.get('display') or '').strip()
                if not text:
                    continue
                slug = text[:40] + ('...' if len(text) > 40 else '')
                cid = d.get('conversationId')
                if cid and cid not in out:
                    out[cid] = slug
                ws = d.get('workspace')
                if ws:
                    out[ws] = slug          # workspace 键取最后一次输入（最近会话兜底）
    except Exception:
        pass
    return out


# ── 历史会话读取 ──────────────────────────────────────────────────────

def _get_history() -> List[Dict[str, Any]]:
    """conversations/*.db 中 age >= 24h 且 N 天内的已结束会话。"""
    sessions = []
    if not CONV_DIR.is_dir():
        return sessions
    now = time.time()
    cutoff = now - HISTORY_DAYS * 86400
    title_map = _history_title_map()

    for db in CONV_DIR.glob('*.db'):
        try:
            mtime = db.stat().st_mtime
            age = int(now - mtime)
            if age < 86400 or mtime < cutoff:
                continue
            meta = _db_meta(db, mtime)
            cwd = meta.get('workspace', '')
            conv_id = db.stem
            sessions.append({
                'session_id' : conv_id,
                'slug'       : title_map.get(conv_id) or title_map.get(cwd) or conv_id[:8],
                'project'    : os.path.basename(cwd) if cwd else '未知项目',
                'cwd'        : cwd or '未知目录',
                'status'     : 'idle',
                'last_tool'  : meta.get('last_tool'),
                'age_seconds': age,
                'runtime'    : datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M'),
                'is_live'    : False,
                'source'     : 'agy',
            })
        except Exception:
            continue

    sessions.sort(key=lambda x: x['age_seconds'])
    return sessions


# ── 工具函数 ──────────────────────────────────────────────────────────

def _has_tree_cpu_activity(pid: str, sample_seconds: float) -> bool:
    try:
        t1 = _tree_cpu_ticks(pid)
        time.sleep(sample_seconds)
        return _tree_cpu_ticks(pid) > t1
    except Exception:
        return False


def _tree_cpu_ticks(root_pid: str) -> int:
    total = 0
    pending = [str(root_pid)]
    seen: set = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f'/proc/{pid}/stat') as f:
                stat = f.read().split()
            total += int(stat[13]) + int(stat[14])
            children = Path(f'/proc/{pid}/task/{pid}/children')
            if children.exists():
                pending.extend(children.read_text().split())
        except Exception:
            continue
    return total


def _get_proc_cwd(pid: str) -> str:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return ''


def _get_age_seconds(pid: str) -> int:
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read().split()
        ticks  = int(stat[21])
        hz     = os.sysconf('SC_CLK_TCK')
        uptime = _get_system_uptime()
        return max(0, int(uptime - ticks / hz))
    except Exception:
        return 0


def _get_proc_runtime(pid: str) -> str:
    s = _get_age_seconds(pid)
    if s < 60:
        return f'{s}s'
    if s < 3600:
        return f'{s // 60}m'
    return f'{s // 3600}h'


def _get_system_uptime() -> float:
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


if __name__ == '__main__':
    result = get_agy_sessions()
    live = [s for s in result['sessions'] if s['is_live']]
    hist = [s for s in result['sessions'] if not s['is_live']]
    print(f"AGY 实时: {len(live)} 个 | 历史: {len(hist)} 个")
    for s in live:
        print(f"  LIVE [{s['session_id'][:8]}] {s['slug']!r} cwd={s['cwd']} "
              f"status={s['status']} tool={s['last_tool']} run={s['runtime']}")
    for s in hist[:5]:
        print(f"  HIST [{s['session_id'][:8]}] {s['slug']!r} cwd={s['cwd']} {s['runtime']}")
