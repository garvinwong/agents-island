"""
codex_monitor.py — 监控运行中的 Codex CLI 进程 + 读取历史会话
- 实时：ps aux 扫描存活进程，按 cwd + 启动时间匹配 rollout
- 历史：~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

CODEX_DIR = Path.home() / '.codex'
CODEX_SESS_DIR = CODEX_DIR / 'sessions'
CODEX_INDEX = CODEX_DIR / 'session_index.jsonl'
HISTORY_DAYS = 7
LIVE_MATCH_WINDOW = 20 * 60
ACTIVE_WRITE_WINDOW = 8
PERM_QUEUE_FILE = Path('/tmp/claude_perm_queue.jsonl')
PERM_RESP_DIR = Path('/tmp/claude_perm_responses')


def get_codex_sessions() -> Dict[str, Any]:
    """扫描实时进程 + 读取历史，返回合并后的会话列表。"""
    index_map = _load_session_index()
    rollouts = _collect_rollout_records(index_map)
    live_sessions = _scan_live_processes(rollouts, index_map)
    history = _get_history(rollouts)

    live_ids = {s['session_id'] for s in live_sessions}
    hist_filtered = [s for s in history if s['session_id'] not in live_ids]

    return {
        'total': len(live_sessions),
        'sessions': live_sessions + hist_filtered,
    }


def _scan_live_processes(rollouts: List[Dict[str, Any]], index_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    session_map: Dict[str, Dict[str, Any]] = {}
    try:
        result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
        for line in result.stdout.splitlines():
            session = _parse_codex_process(line, rollouts, index_map)
            if not session:
                continue
            key = session['session_id']
            prev = session_map.get(key)
            if not prev:
                session_map[key] = session
                continue

            # 同一 session 命中多个进程时，保留更像主会话的那条：
            # 1. cwd 有效优先
            # 2. age 更小优先（更接近前台会话）
            prev_score = (0 if prev.get('cwd') and prev.get('cwd') != '未知目录' else 1, prev.get('age_seconds', 0))
            curr_score = (0 if session.get('cwd') and session.get('cwd') != '未知目录' else 1, session.get('age_seconds', 0))
            if curr_score < prev_score:
                session_map[key] = session
    except Exception:
        pass

    sessions = list(session_map.values())
    sessions.sort(key=lambda s: s.get('age_seconds', 0))
    return sessions


def _parse_codex_process(ps_line: str, rollouts: List[Dict[str, Any]], index_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any] | None:
    parts = ps_line.split(None, 10)
    if len(parts) < 11:
        return None

    pid = parts[1]
    cmd = parts[10]
    cmd_l = cmd.lower()

    if 'grep' in cmd_l or 'codex_monitor.py' in cmd_l:
        return None
    if 'codex' not in cmd_l:
        return None
    if not any(token in cmd_l for token in ['node', '/vendor/', 'codex resume', 'codex-linux-sandbox']):
        return None

    cwd = _get_proc_cwd(pid)
    age_seconds = _get_age_seconds(pid)
    started_at = time.time() - age_seconds

    match = _find_live_session_info(cwd, started_at, rollouts)
    pending_perm = _has_pending_permission(match.get('session_id') if match else None, pid)
    status = _detect_codex_status(pid, match, pending_perm)
    session_id = match.get('session_id') if match else None

    slug = None
    if session_id and session_id in index_map:
        slug = index_map[session_id].get('thread_name') or None
    if not slug and match:
        slug = match.get('slug')

    last_tool = None
    if pending_perm:
        last_tool = 'Bash'
    elif match:
        last_tool = match.get('last_tool')

    return {
        'session_id': session_id or f'codex-{pid}',
        'pid': pid,
        'slug': slug or f'Codex Session ({pid})',
        'project': _project_from_cwd(match.get('cwd') if match else cwd),
        'cwd': (match.get('cwd') if match and match.get('cwd') else cwd) or '未知目录',
        'status': status,
        'last_tool': last_tool,
        'age_seconds': age_seconds,
        'runtime': _get_proc_runtime(pid),
        'is_live': True,
        'source': 'codex',
    }


def _find_live_session_info(cwd: str, started_at: float, rollouts: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """按 cwd + 启动时间关联最可能的 rollout，避免多开时全部命中当天最新文件。"""
    candidates = [r for r in rollouts if r.get('session_id')]
    if cwd:
        cwd_matches = [r for r in candidates if r.get('cwd') == cwd]
        if cwd_matches:
            candidates = cwd_matches

    if not candidates:
        return None

    def score(rec: Dict[str, Any]) -> tuple[int, float, float]:
        rec_started = rec.get('started_at') or rec.get('mtime') or 0.0
        rec_updated = rec.get('updated_at') or rec.get('mtime') or 0.0
        exact_cwd = 0 if rec.get('cwd') == cwd and cwd else 1
        start_gap = abs(rec_started - started_at)
        update_gap = abs(rec_updated - started_at)
        return (exact_cwd, start_gap, update_gap)

    close = [
        r for r in candidates
        if abs((r.get('started_at') or r.get('mtime') or 0.0) - started_at) <= LIVE_MATCH_WINDOW
    ]
    pool = close or candidates
    pool.sort(key=score)
    return pool[0] if pool else None


def _collect_rollout_records(index_map: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    records = []
    if not CODEX_SESS_DIR.exists():
        return records

    now = time.time()
    for i in range(HISTORY_DAYS):
        ts = now - i * 86400
        dt = datetime.fromtimestamp(ts)
        day_dir = CODEX_SESS_DIR / dt.strftime('%Y/%m/%d')
        if not day_dir.exists():
            continue

        for file_path in day_dir.glob('rollout-*.jsonl'):
            record = _parse_rollout_record(file_path, index_map)
            if record:
                records.append(record)

    latest_by_session: Dict[str, Dict[str, Any]] = {}
    fallback_records: List[Dict[str, Any]] = []
    for rec in records:
        sid = rec.get('session_id')
        if not sid:
            fallback_records.append(rec)
            continue
        prev = latest_by_session.get(sid)
        if not prev or rec.get('mtime', 0) >= prev.get('mtime', 0):
            latest_by_session[sid] = rec

    merged = list(latest_by_session.values()) + fallback_records
    merged.sort(key=lambda r: r.get('mtime', 0), reverse=True)
    return merged


def _parse_rollout_record(file_path: Path, index_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any] | None:
    session_id = None
    cwd = ''
    first_user = None
    started_at = None
    last_tool = None

    try:
        with open(file_path, 'r', errors='replace') as f:
            for line in f:
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                item_type = data.get('type')
                if item_type == 'session_meta':
                    payload = data.get('payload', {})
                    session_id = payload.get('id') or session_id
                    cwd = payload.get('cwd') or cwd
                    started_at = _parse_iso_time(payload.get('timestamp')) or started_at
                elif item_type == 'response_item' and not first_user:
                    payload = data.get('payload', {})
                    if payload.get('role') == 'user':
                        first_user = _extract_user_text(payload.get('content', [])) or first_user

                tool_name = _extract_codex_tool_label(data)
                if tool_name:
                    last_tool = tool_name

                if session_id and cwd and first_user and started_at:
                    continue
    except Exception:
        return None

    try:
        stat = file_path.stat()
    except Exception:
        return None

    idx = index_map.get(session_id or '', {})
    thread_name = idx.get('thread_name') or None
    updated_at = _parse_iso_time(idx.get('updated_at')) if idx else None

    return {
        'session_id': session_id,
        'slug': thread_name or first_user or (session_id[:8] if session_id else file_path.stem[-8:]),
        'thread_name': thread_name,
        'cwd': cwd,
        'project': _project_from_cwd(cwd),
        'file': str(file_path),
        'mtime': stat.st_mtime,
        'started_at': started_at or stat.st_mtime,
        'updated_at': updated_at or stat.st_mtime,
        'last_tool': last_tool,
    }


def _load_session_index() -> Dict[str, Dict[str, Any]]:
    index_map: Dict[str, Dict[str, Any]] = {}
    if not CODEX_INDEX.exists():
        return index_map

    try:
        with open(CODEX_INDEX, 'r', errors='replace') as f:
            for line in f:
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                sid = data.get('id')
                if not sid:
                    continue
                index_map[sid] = data
    except Exception:
        pass
    return index_map


def _extract_user_text(content: Any) -> str | None:
    if isinstance(content, str):
        text = content.strip()
        return _clean_title(text) if text else None

    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get('text') or block.get('input_text')
            cleaned = _clean_title(text)
            if cleaned:
                return cleaned
    return None


def _clean_title(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.startswith('<environment_context>') or cleaned.startswith('<turn_aborted>'):
        return None
    cleaned = cleaned.replace('\n', ' ')
    return cleaned[:40].strip() + ('...' if len(cleaned) > 40 else '')


def _detect_codex_status(pid: str, rollout: Dict[str, Any] | None = None, pending_permission: bool = False) -> str:
    """结合进程树 CPU 活动和 rollout 最近写入时间判定 working。"""
    if pending_permission:
        return 'waiting_permission'
    if _has_tree_cpu_activity(pid, 0.2):
        return 'executing_tool'

    if rollout:
        updated_at = rollout.get('updated_at') or rollout.get('mtime') or 0
        if updated_at and (time.time() - updated_at) <= ACTIVE_WRITE_WINDOW:
            return 'executing_tool'
    return 'standby'


def _has_tree_cpu_activity(pid: str, sample_seconds: float) -> bool:
    try:
        t1 = _get_process_tree_cpu_ticks(pid)
        time.sleep(sample_seconds)
        t2 = _get_process_tree_cpu_ticks(pid)
        return t2 > t1
    except Exception:
        return False


def _get_process_tree_cpu_ticks(root_pid: str) -> int:
    total = 0
    pending = [str(root_pid)]
    seen: set[str] = set()

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


def _get_history(rollouts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sessions = []
    now = time.time()

    for rec in rollouts:
        age_seconds = int(now - rec.get('mtime', now))
        if age_seconds < 86400:
            continue

        updated_at = rec.get('updated_at') or rec.get('mtime') or now
        sessions.append({
            'session_id': rec.get('session_id') or Path(rec.get('file', '')).stem,
            'slug': rec.get('slug') or 'Codex Session',
            'project': rec.get('project') or 'Historical Codex',
            'cwd': rec.get('cwd') or 'N/A',
            'status': 'idle',
            'last_tool': rec.get('last_tool'),
            'age_seconds': age_seconds,
            'runtime': datetime.fromtimestamp(updated_at).strftime('%m-%d %H:%M'),
            'is_live': False,
            'source': 'codex',
        })

    sessions.sort(key=lambda x: x['age_seconds'])
    return sessions


def _parse_iso_time(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def _project_from_cwd(cwd: str | None) -> str:
    if not cwd:
        return 'Codex Workspace'
    base = os.path.basename(cwd.rstrip('/'))
    return base or 'Codex Workspace'


def _extract_codex_tool_label(row: Dict[str, Any]) -> str | None:
    payload = row.get('payload', {}) if isinstance(row, dict) else {}
    if row.get('type') == 'response_item' and payload.get('type') == 'function_call':
        return _normalize_tool_name(payload.get('name'))
    return None


def _normalize_tool_name(name: str | None) -> str | None:
    if not name:
        return None
    mapping = {
        'exec_command': 'Bash',
        'write_stdin': 'Terminal',
        'apply_patch': 'Patch',
        'view_image': 'Image',
        'search_query': 'Web',
        'open': 'Open',
        'click': 'Click',
        'find': 'Find',
        'read_mcp_resource': 'MCP',
        'exec_command_output': 'Bash',
    }
    normalized = mapping.get(name)
    if normalized:
        return normalized
    return str(name).replace('_', ' ')[:18]


def _has_pending_permission(session_id: str | None, pid: str | None = None) -> bool:
    if not PERM_QUEUE_FILE.exists():
        return False
    try:
        for line in PERM_QUEUE_FILE.read_text(errors='replace').splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get('agent_source') != 'codex' and not str(item.get('id', '')).startswith('codex_'):
                continue
            item_sid = item.get('session_id')
            if session_id and item_sid and item_sid != session_id:
                continue
            perm_id = str(item.get('id') or '')
            if perm_id and (PERM_RESP_DIR / f'{perm_id}.json').exists():
                continue
            if item_sid or session_id:
                return True
            if pid:
                return True
    except Exception:
        return False
    return False


def _get_proc_cwd(pid: str) -> str:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return ''


def _get_age_seconds(pid: str) -> int:
    try:
        with open(f'/proc/{pid}/stat') as f:
            ticks = int(f.read().split()[21])
        hz = os.sysconf('SC_CLK_TCK')
        uptime = _get_system_uptime()
        return max(0, int(uptime - ticks / hz))
    except Exception:
        return 0


def _get_proc_runtime(pid: str) -> str:
    age = _get_age_seconds(pid)
    if age < 60:
        return f'{age}s'
    if age < 3600:
        return f'{age // 60}m'
    return f'{age // 3600}h'


def _get_system_uptime() -> float:
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


if __name__ == '__main__':
    res = get_codex_sessions()
    print(f"Codex: {res['total']} live")
    for s in res['sessions'][:5]:
        print(f"  {s['slug']} :: {s['cwd']}")
