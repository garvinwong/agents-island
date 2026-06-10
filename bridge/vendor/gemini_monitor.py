"""
gemini_monitor.py — 监控运行中的 Gemini CLI 进程 + 读取历史会话
- 实时：ps aux 扫描存活进程
- 历史：~/.gemini/tmp/*/chats/session-*.json（age >= 86400s 的已结束会话）
"""
import subprocess
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

GEMINI_TMP_DIR = Path.home() / '.gemini' / 'tmp'
HISTORY_DAYS   = 7   # 读取最近 N 天的历史
ACTIVE_WRITE_WINDOW = 8


def get_gemini_sessions() -> Dict[str, Any]:
    """扫描实时进程 + 读取历史，返回合并后的会话列表。"""
    live_sessions = _scan_live_processes()
    history       = _get_history()

    # 历史中已在实时列表里的 session_id 去重（以实时数据为准）
    live_ids = {s['session_id'] for s in live_sessions}
    hist_filtered = [s for s in history if s['session_id'] not in live_ids]

    return {
        'total'   : len(live_sessions),
        'sessions': live_sessions + hist_filtered,
    }


# ── 实时进程扫描 ──────────────────────────────────────────────────────

def _scan_live_processes() -> List[Dict[str, Any]]:
    session_map: Dict[str, Dict[str, Any]] = {}
    try:
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            session = _parse_gemini_process(line)
            if session:
                key = session['session_id']
                prev = session_map.get(key)
                if not prev:
                    session_map[key] = session
                    continue

                prev_score = (
                    0 if prev.get('cwd') and prev.get('cwd') != '未知目录' else 1,
                    prev.get('age_seconds', 0),
                )
                curr_score = (
                    0 if session.get('cwd') and session.get('cwd') != '未知目录' else 1,
                    session.get('age_seconds', 0),
                )
                if curr_score < prev_score:
                    session_map[key] = session
    except Exception:
        pass
    sessions = list(session_map.values())
    sessions.sort(key=lambda s: s.get('age_seconds', 0))
    return sessions


def _parse_gemini_process(ps_line: str) -> Dict[str, Any] | None:
    """从 ps 输出行解析 Gemini CLI 进程信息"""
    GEMINI_PATTERNS = [
        r'gemini(\s|$)',
        r'gemini-cli',
        r'@google/gemini',
        r'gemini\.js',
        r'/bin/gemini\b',
    ]
    matched = any(re.search(p, ps_line, re.IGNORECASE) for p in GEMINI_PATTERNS)
    if not matched:
        return None
    if 'grep' in ps_line:
        return None

    parts = ps_line.split(None, 10)
    if len(parts) < 11:
        return None

    pid = parts[1]
    cmd = parts[10] if len(parts) > 10 else ''
    cwd = _get_proc_cwd(pid)
    project = _extract_project_from_cmd(cmd, cwd)

    session_info = _find_live_session_info(pid, cwd)
    status = _detect_gemini_status(pid, session_info)

    return {
        'session_id' : session_info.get('session_id') or f'gemini-{pid}',
        'pid'        : pid,
        'slug'       : session_info.get('slug') or f'gemini-{pid}',
        'project'    : project,
        'cwd'        : cwd or '未知目录',
        'status'     : status,
        'last_tool'  : session_info.get('last_tool'),
        'age_seconds': _get_age_seconds(pid),
        'runtime'    : _get_proc_runtime(pid),
        'is_live'    : True,
        'source'     : 'gemini',
    }


def _extract_first_user_msg(data: Dict[str, Any]) -> str | None:
    """从 session JSON 数据中提取第一条用户消息作为标题"""
    try:
        for msg in data.get('messages', []):
            if msg.get('type') == 'user':
                content = msg.get('content', [])
                if isinstance(content, list) and len(content) > 0:
                    text = content[0].get('text', '')
                    if text:
                        return text[:40].strip() + ('...' if len(text) > 40 else '')
    except: pass
    return None


def _find_live_session_info(pid: str, cwd: str) -> Dict[str, Any]:
    """在 ~/.gemini/tmp/ 下按 cwd 优先寻找属于该 live 进程的 session 文件。"""
    try:
        if not GEMINI_TMP_DIR.exists():
            return {}

        candidates = []
        for session_file in GEMINI_TMP_DIR.glob('**/chats/session-*.json'):
            try:
                stat = session_file.stat()
            except Exception:
                continue

            workspace_dir = session_file.parent.parent
            project_root = ''
            project_root_file = workspace_dir / '.project_root'
            if project_root_file.exists():
                try:
                    project_root = project_root_file.read_text().strip()
                except Exception:
                    project_root = ''

            candidates.append({
                'file': session_file,
                'mtime': stat.st_mtime,
                'cwd_match': 0 if cwd and project_root == cwd else 1,
            })

        if not candidates:
            return {}

        candidates.sort(key=lambda item: (item['cwd_match'], -item['mtime']))
        latest = candidates[0]['file']

        with open(latest) as f:
            data = json.load(f)

        return {
            'session_id': data.get('sessionId'),
            'slug': _extract_first_user_msg(data),
            'file': str(latest),
            'mtime': latest.stat().st_mtime,
            'last_updated': _parse_iso_time(data.get('lastUpdated', data.get('startTime', ''))) or latest.stat().st_mtime,
            'last_tool': _extract_recent_action_label(data),
        }
    except Exception:
        pass
    return {}


def _detect_gemini_status(pid: str, session_info: Dict[str, Any] | None = None) -> str:
    """结合进程树 CPU 活动和 session 文件最近写入时间判定 working。"""
    if _has_tree_cpu_activity(pid, 0.2):
        return 'executing_tool'

    if session_info:
        updated_at = session_info.get('last_updated') or session_info.get('mtime') or 0
        if updated_at and (time.time() - updated_at) <= ACTIVE_WRITE_WINDOW:
            return 'executing_tool'
    return 'standby'


def _extract_recent_action_label(data: Dict[str, Any]) -> str | None:
    messages = data.get('messages', [])
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get('toolCalls') or []
        if isinstance(tool_calls, list) and tool_calls:
            last_call = tool_calls[-1]
            if isinstance(last_call, dict):
                return _normalize_gemini_tool_name(last_call.get('name'))
        if msg.get('type') == 'gemini':
            content = str(msg.get('content') or '').strip()
            if content:
                return 'Thinking'
    return None


def _normalize_gemini_tool_name(name: str | None) -> str | None:
    if not name:
        return None
    mapping = {
        'write_file': 'Write',
        'read_file': 'Read',
        'run_shell_command': 'Bash',
        'list_directory': 'List',
        'search_file_content': 'Search',
        'replace_text_in_file': 'Patch',
        'google_web_search': 'Web',
    }
    normalized = mapping.get(name)
    if normalized:
        return normalized
    return str(name).replace('_', ' ')[:18]


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


# ── 历史会话读取 ──────────────────────────────────────────────────────

def _get_history() -> List[Dict[str, Any]]:
    """从 ~/.gemini/tmp/*/chats/session-*.json 读取历史会话（age >= 86400s）。"""
    sessions = []
    now    = time.time()
    cutoff = now - HISTORY_DAYS * 86400

    if not GEMINI_TMP_DIR.exists():
        return sessions

    for ws_dir in GEMINI_TMP_DIR.iterdir():
        if not ws_dir.is_dir():
            continue

        # 工作区真实路径
        project_root_file = ws_dir / '.project_root'
        cwd = ''
        if project_root_file.exists():
            try:
                cwd = project_root_file.read_text().strip()
            except Exception:
                pass

        chats_dir = ws_dir / 'chats'
        if not chats_dir.exists():
            continue

        for session_file in chats_dir.glob('session-*.json'):
            try:
                with open(session_file) as f:
                    data = json.load(f)

                session_id     = data.get('sessionId', '')
                start_time_str = data.get('startTime', '')
                last_updated   = _parse_iso_time(data.get('lastUpdated', start_time_str))
                if last_updated is None:
                    continue

                age_seconds = int(now - last_updated)

                # 只要 age >= 24h 且在 N 天内
                if age_seconds < 86400 or last_updated < cutoff:
                    continue

                # 计算会话持续时间
                start_ts = _parse_iso_time(start_time_str)
                if start_ts and last_updated > start_ts:
                    dur = int(last_updated - start_ts)
                    if dur < 60:
                        runtime = f'{dur}s'
                    elif dur < 3600:
                        runtime = f'{dur // 60}m'
                    else:
                        runtime = f'{dur // 3600}h{(dur % 3600) // 60}m'
                else:
                    runtime = '?'

                project   = os.path.basename(cwd) if cwd else ws_dir.name
                msg_count = len(data.get('messages', []))
                
                # 获取首句输入作为标题
                slug = _extract_first_user_msg(data) or (session_id[:8] if session_id else ws_dir.name)

                sessions.append({
                    'session_id'   : session_id,
                    'slug'         : slug,
                    'project'      : project,
                    'cwd'          : cwd or ws_dir.name,
                    'status'       : 'idle',
                    'last_tool'    : _extract_recent_action_label(data),
                    'age_seconds'  : age_seconds,
                    'runtime'      : runtime,
                    'message_count': msg_count,
                    'is_live'      : False,
                    'source'       : 'gemini',
                })
            except Exception:
                continue

    sessions.sort(key=lambda x: x['age_seconds'])
    return sessions


# ── 工具函数 ──────────────────────────────────────────────────────────

def _parse_iso_time(s: str) -> float | None:
    if not s:
        return None
    try:
        s = s.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except Exception:
        return None


def _get_age_seconds(pid: str) -> int:
    """从 /proc/{pid}/stat 获取进程已存活秒数。"""
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read().split()
        ticks   = int(stat[21])
        hz      = os.sysconf('SC_CLK_TCK')
        uptime  = _get_system_uptime()
        return max(0, int(uptime - ticks / hz))
    except Exception:
        return 0


def _get_proc_cwd(pid: str) -> str:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return ''


def _get_proc_runtime(pid: str) -> str:
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read().split()
        ticks      = int(stat[21])
        hz         = os.sysconf('SC_CLK_TCK')
        uptime     = _get_system_uptime()
        runtime_s  = int(uptime - ticks / hz)
        if runtime_s < 60:
            return f'{runtime_s}s'
        if runtime_s < 3600:
            return f'{runtime_s // 60}m'
        return f'{runtime_s // 3600}h'
    except Exception:
        return '?'


def _get_system_uptime() -> float:
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def _extract_project_from_cmd(cmd: str, cwd: str) -> str:
    if cwd:
        return os.path.basename(cwd) or cwd
    parts = cmd.split()
    for part in reversed(parts):
        if '/' in part and os.path.exists(part):
            return os.path.basename(part)
    return 'gemini-project'


if __name__ == '__main__':
    result = get_gemini_sessions()
    live   = [s for s in result['sessions'] if s['age_seconds'] < 86400]
    hist   = [s for s in result['sessions'] if s['age_seconds'] >= 86400]
    print(f"Gemini 实时: {len(live)} 个 | 历史: {len(hist)} 个")
    for s in hist[:5]:
        print(f"  [{s['session_id'][:8]}] {s['cwd']}  {s['runtime']}  age={s['age_seconds']//3600}h")
