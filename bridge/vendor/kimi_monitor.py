"""
kimi_monitor.py — 监控运行中的 Kimi CLI 进程 + 读取历史会话
- 实时：ps aux 扫描存活进程
- 历史：~/.kimi/logs/kimi*.log（解析 "Created new session" 行）
"""
import subprocess
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

KIMI_LOG_DIR  = Path.home() / '.kimi' / 'logs'
KIMI_CONFIG   = Path.home() / '.kimi' / 'kimi.json'
HISTORY_DAYS  = 7   # 读取最近 N 天的历史


def get_kimi_sessions() -> Dict[str, Any]:
    """扫描实时进程 + 读取历史，返回合并后的会话列表。"""
    live_sessions = _scan_live_processes()
    history       = _get_history()

    # 历史 session_id 为 UUID，实时为 kimi-{pid}，天然不重复；
    # 但若历史中有 age < 86400 的（今天启动但已结束），需按 uuid 过滤
    live_ids = {s['session_id'] for s in live_sessions}
    hist_filtered = [s for s in history if s['session_id'] not in live_ids]

    return {
        'total'   : len(live_sessions),
        'sessions': live_sessions + hist_filtered,
    }


# ── 实时进程扫描 ──────────────────────────────────────────────────────

def _scan_live_processes() -> List[Dict[str, Any]]:
    sessions = []
    try:
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            session = _parse_kimi_process(line)
            if session:
                sessions.append(session)
    except Exception:
        pass
    return sessions


def _parse_kimi_process(ps_line: str) -> Dict[str, Any] | None:
    """从 ps 输出行解析 Kimi CLI 进程信息（只检查 exe basename）"""
    parts = ps_line.split(None, 10)
    if len(parts) < 11:
        return None

    pid = parts[1]
    cmd = parts[10] if len(parts) > 10 else ''

    exe      = cmd.split()[0] if cmd.strip() else ''
    exe_base = os.path.basename(exe)

    KIMI_PATTERNS = [
        r'^kimi$',
        r'^kimi-cli$',
        r'^kimi_cli$',
        r'@moonshot-ai/kimi',
    ]
    if not any(re.search(p, exe_base, re.IGNORECASE) for p in KIMI_PATTERNS):
        return None

    if 'grep' in ps_line or 'kimi_monitor' in ps_line:
        return None

    cwd     = _get_proc_cwd(pid)
    project = _extract_project_from_cmd(cmd, cwd)

    # 状态感知
    status = _detect_kimi_status(pid)

    # 获取语义化标题
    session_id, slug, last_tool, context_pct = _find_live_session_info(pid, cwd)

    return {
        'session_id' : session_id or f'kimi-{pid}',
        'pid'        : pid,
        'slug'       : slug or f'kimi-{pid}',
        'project'    : project,
        'cwd'        : cwd or '未知目录',
        'status'     : status,
        'last_tool'  : last_tool,
        'context_pct': context_pct,   # 上下文占用 %（开新任务/逼近压缩的决策输入）
        'age_seconds': _get_age_seconds(pid),
        'runtime'    : _get_proc_runtime(pid),
        'is_live'    : True,
        'source'     : 'kimi',
    }


def _find_live_session_info(pid: str, cwd: str) -> tuple[str | None, str | None, str | None, int | None]:
    """在 ~/.kimi/sessions 下寻找属于该 PID 的最新 session 文件。"""
    try:
        KIMI_SESS_DIR = Path.home() / '.kimi' / 'sessions'
        if not KIMI_SESS_DIR.exists():
            return None, None, None, None

        # 寻找最近变动的 wire.jsonl
        candidate_files = list(KIMI_SESS_DIR.glob('**/wire.jsonl'))
        if not candidate_files:
            return None, None, None, None

        latest = max(candidate_files, key=lambda p: p.stat().st_mtime)

        session_dir = latest.parent
        slug, last_tool = _parse_kimi_session_summary(session_dir)
        context_pct = _read_context_pct(latest)

        # 获取 sessionId (从同级目录的 state.json)
        sid = session_dir.name
        return sid, slug, last_tool, context_pct
    except: pass
    return None, None, None, None


def _read_context_pct(wire_file: Path) -> int | None:
    """从 wire.jsonl 尾部 64KB 取最近一条 StatusUpdate 的 context_usage（0~1）→ 百分比。"""
    try:
        size = wire_file.stat().st_size
        with open(wire_file, 'rb') as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode(errors='replace')
        for line in reversed(tail.splitlines()):
            if '"context_usage"' not in line:
                continue
            try:
                payload = (json.loads(line).get('message') or {}).get('payload') or {}
            except Exception:
                continue
            usage = payload.get('context_usage')
            if isinstance(usage, (int, float)):
                return max(0, min(100, round(usage * 100)))
        return None
    except Exception:
        return None


def _detect_kimi_status(pid: str) -> str:
    """探测 Kimi 活跃状态"""
    try:
        def _get_cpu(p):
            with open(f'/proc/{p}/stat') as f:
                s = f.read().split()
                return int(s[13]) + int(s[14])
        t1 = _get_cpu(pid)
        time.sleep(0.1)
        t2 = _get_cpu(pid)
        if t2 > t1: return 'executing_tool'
        
        # 检查子进程
        children = subprocess.run(['pgrep', '-P', pid], capture_output=True).stdout.strip()
        if children: return 'executing_tool'
    except: pass
    return 'standby'


# ── 历史会话读取 ──────────────────────────────────────────────────────

def _get_history() -> List[Dict[str, Any]]:
    """
    从 ~/.kimi/logs/kimi*.log 解析历史会话。
    日志格式: '2026-04-08 08:13:45.613 | INFO | ... Created new session: <uuid>'
    工作目录映射: ~/.kimi/kimi.json work_dirs[].last_session_id → path
    """
    sessions = []
    now      = time.time()
    cutoff   = now - HISTORY_DAYS * 86400

    if not KIMI_LOG_DIR.exists():
        return sessions

    # 从 kimi.json 建立 session_id → cwd 映射（仅 last_session_id 有记录）
    sid_to_cwd: Dict[str, str] = {}
    try:
        with open(KIMI_CONFIG) as f:
            config = json.load(f)
        for wd in config.get('work_dirs', []):
            sid  = wd.get('last_session_id', '')
            path = wd.get('path', '')
            if sid and path:
                sid_to_cwd[sid] = path
    except Exception:
        pass

    # 扫描所有日志文件，提取 session 创建记录
    seen_ids: set = set()
    log_sessions: Dict[str, Dict] = {}  # session_id → {time, cwd}

    for log_file in sorted(KIMI_LOG_DIR.glob('kimi*.log')):
        try:
            with open(log_file, errors='replace') as f:
                for line in f:
                    if 'Created new session:' not in line:
                        continue
                    parts = line.split('Created new session:')
                    if len(parts) < 2:
                        continue
                    session_id = parts[1].strip()
                    if not session_id or session_id in seen_ids:
                        continue
                    seen_ids.add(session_id)

                    ts = _parse_log_time(line)
                    if ts is None or ts < cutoff:
                        continue

                    cwd = sid_to_cwd.get(session_id, '')
                    log_sessions[session_id] = {'time': ts, 'cwd': cwd}
        except Exception:
            continue

    for session_id, info in log_sessions.items():
        age_seconds = int(now - info['time'])
        if age_seconds < 86400:
            continue  # 今天的会话，跳过（进程若还在会显示在监控 tab）

        cwd     = info['cwd']
        project = os.path.basename(cwd) if cwd else '未知项目'
        ts_str  = datetime.fromtimestamp(info['time']).strftime('%m-%d %H:%M')
        slug, last_tool = _load_history_session_details(session_id)

        sessions.append({
            'session_id' : session_id,
            'slug'       : slug or session_id[:8],
            'project'    : project,
            'cwd'        : cwd or '未知目录',
            'status'     : 'idle',
            'last_tool'  : last_tool,
            'age_seconds': age_seconds,
            'runtime'    : ts_str,   # Kimi 日志无结束时间，用启动时间代替
            'is_live'    : False,
            'source'     : 'kimi',
        })

    sessions.sort(key=lambda x: x['age_seconds'])
    return sessions


# ── 工具函数 ──────────────────────────────────────────────────────────

def _parse_log_time(line: str) -> float | None:
    """解析 Kimi 日志时间戳：'2026-04-08 08:13:45.613 | ...'"""
    try:
        ts_str = line.split('|')[0].strip()
        dt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S.%f')
        return dt.timestamp()
    except Exception:
        return None


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


def _get_proc_cwd(pid: str) -> str:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except Exception:
        return ''


def _get_proc_runtime(pid: str) -> str:
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read().split()
        ticks     = int(stat[21])
        hz        = os.sysconf('SC_CLK_TCK')
        uptime    = _get_system_uptime()
        runtime_s = int(uptime - ticks / hz)
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
    return 'kimi-project'


def _load_history_session_details(session_id: str) -> tuple[str | None, str | None]:
    try:
        matches = list((Path.home() / '.kimi' / 'sessions').glob(f'**/{session_id}/state.json'))
        if not matches:
            return None, None
        latest = max(matches, key=lambda p: p.stat().st_mtime)
        return _parse_kimi_session_summary(latest.parent)
    except Exception:
        return None, None


def _parse_kimi_session_summary(session_dir: Path) -> tuple[str | None, str | None]:
    slug = _read_kimi_custom_title(session_dir / 'state.json')
    wire_file = session_dir / 'wire.jsonl'
    last_tool = None
    if not wire_file.exists():
        return slug, last_tool

    try:
        with open(wire_file, errors='replace') as f:
            for line in f:
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                msg = data.get('message') or {}
                msg_type = msg.get('type')
                payload = msg.get('payload') or {}

                if not slug and msg_type == 'TurnBegin':
                    text = _extract_turnbegin_text(payload.get('user_input'))
                    if text:
                        slug = text[:40].strip() + ('...' if len(text) > 40 else '')

                tool_name = _extract_kimi_tool_label(msg_type, payload)
                if tool_name:
                    last_tool = tool_name
        return slug, last_tool
    except Exception:
        return slug, last_tool


def _read_kimi_custom_title(state_file: Path) -> str | None:
    try:
        data = json.loads(state_file.read_text(encoding='utf-8'))
    except Exception:
        return None

    title = str(data.get('custom_title') or '').strip()
    if not title:
        return None
    return title


def _extract_turnbegin_text(user_input: Any) -> str | None:
    if isinstance(user_input, str):
        return user_input.strip() or None
    if isinstance(user_input, list):
        for item in user_input:
            if isinstance(item, dict):
                text = str(item.get('text', '')).strip()
                if text:
                    return text
            elif isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _extract_kimi_tool_label(msg_type: str | None, payload: dict) -> str | None:
    if msg_type != 'ToolCall':
        return None

    func = payload.get('function') or {}
    tool_name = func.get('name') or payload.get('name') or payload.get('tool_name')
    if not tool_name:
        return 'Tool'
    return _normalize_kimi_tool_name(str(tool_name))


def _normalize_kimi_tool_name(name: str) -> str:
    n = name.strip().lower()
    mappings = [
        ('multiedit', 'Patch'),
        ('applypatch', 'Patch'),
        ('patch', 'Patch'),
        ('edit', 'Edit'),
        ('write', 'Write'),
        ('create', 'Write'),
        ('read', 'Read'),
        ('open', 'Read'),
        ('glob', 'Search'),
        ('grep', 'Search'),
        ('search', 'Search'),
        ('list', 'List'),
        ('ls', 'List'),
        ('bash', 'Bash'),
        ('shell', 'Bash'),
        ('exec', 'Bash'),
        ('terminal', 'Terminal'),
        ('todo', 'Plan'),
        ('web', 'Web'),
        ('browser', 'Web'),
        ('fetchurl', 'Web'),
    ]
    for key, label in mappings:
        if key in n:
            return label
    return name[:18]


if __name__ == '__main__':
    result = get_kimi_sessions()
    live   = [s for s in result['sessions'] if s['age_seconds'] < 86400]
    hist   = [s for s in result['sessions'] if s['age_seconds'] >= 86400]
    print(f"Kimi 实时: {len(live)} 个 | 历史: {len(hist)} 个")
    for s in hist[:5]:
        print(f"  [{s['session_id'][:8]}] {s['cwd']}  启动={s['runtime']}  age={s['age_seconds']//3600}h")
