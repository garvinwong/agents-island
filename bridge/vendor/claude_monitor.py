"""
claude_monitor.py — 扫描 ~/.claude/projects/ 目录，返回所有活跃实例状态
从 dynamic-island-desktop/backend/core/claude_monitor.py 精简迁移
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any

# 状态常量
STATUS_THINKING    = 'thinking'
STATUS_EXECUTING   = 'executing_tool'
STATUS_WAITING     = 'waiting_permission'
STATUS_STANDBY     = 'standby'
STATUS_IDLE        = 'idle'
STATUS_DONE        = 'done'

CLAUDE_DIR = Path.home() / '.claude' / 'projects'


def get_all_sessions() -> List[Dict[str, Any]]:
    """扫描所有 JSONL 文件，返回实例列表"""
    sessions = []
    if not CLAUDE_DIR.exists():
        return sessions

    for project_dir in CLAUDE_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in sorted(project_dir.glob('*.jsonl'), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                session = _parse_session(jsonl_file, project_dir.name)
                if session:
                    sessions.append(session)
            except Exception:
                pass

    _merge_live_processes(sessions, _scan_live_processes())

    # 按 age 升序（最近活跃优先）
    sessions.sort(key=lambda s: s['age_seconds'])
    return sessions


def _parse_session(jsonl_file: Path, project_slug: str) -> Dict[str, Any] | None:
    """解析单个 JSONL 文件，返回 session 信息"""
    try:
        stat = jsonl_file.stat()
        age_seconds = int(time.time() - stat.st_mtime)

        # 超过 7 天的不展示（24h 内为活跃，24h-7天为历史）
        if age_seconds > 604800:
            return None

        # 读取最后 30 行
        lines = _tail_lines(jsonl_file, 30)
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except Exception:
                pass

        if not events:
            return None

        status, last_tool, cwd, git_branch = _infer_status(events, age_seconds)

        # 从 JSONL 提取 cwd / git branch / title
        # 优先级：custom-title（/rename 命令） > 第一条用户消息
        title = None
        custom_title = None
        try:
            first_lines = _head_lines(jsonl_file, 15)
            for fl in first_lines:
                try:
                    obj = json.loads(fl)
                    if obj.get('cwd') and not cwd:
                        cwd = obj['cwd']
                    if obj.get('git_branch') and not git_branch:
                        git_branch = obj['git_branch']
                    # /rename 命令写入的 custom-title
                    if obj.get('type') == 'custom-title' and obj.get('customTitle'):
                        custom_title = obj['customTitle'].strip()
                    # 提取第一条用户消息作为备用标题
                    if not title and obj.get('type') == 'user':
                        msg = obj.get('message', {})
                        content = msg.get('content', '')
                        if isinstance(content, str):
                            title = content.strip()
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    title = block.get('text', '').strip()
                                    break
                        if title:
                            title = title.replace('\n', ' ')[:50]
                except Exception:
                    pass

            # custom-title 不在头部时，扫描全文（/rename 可在任意时刻触发）
            if not custom_title:
                try:
                    with open(jsonl_file, 'r', errors='replace') as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                                if obj.get('type') == 'custom-title' and obj.get('customTitle'):
                                    custom_title = obj['customTitle'].strip()
                                    # 取最后一次 rename（覆盖前一次）
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass

        # custom-title 优先
        if custom_title:
            title = custom_title

        # 推断项目名
        project = _slug_to_project(project_slug)

        return {
            'session_id': jsonl_file.stem,
            'slug': jsonl_file.stem[:20],
            'title': title or '',
            'project': project,
            'project_slug': project_slug,
            'cwd': cwd or project,
            'git_branch': git_branch or 'main',
            'status': status,
            'last_tool': last_tool,
            'age_seconds': age_seconds,
            'file': str(jsonl_file),
            'is_live': False,
        }
    except Exception:
        return None


def _scan_live_processes() -> List[Dict[str, Any]]:
    processes = []
    patterns = [
        r'(^|[ /])claude($| )',
        r'claude-code',
        r'@anthropic-ai/claude-code',
    ]
    try:
        result = subprocess.run(
            ['ps', '-eo', 'pid=,args='],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(None, 1)
            if len(parts) < 2:
                continue
            pid, cmd = parts[0], parts[1]
            cmd_l = cmd.lower()
            if 'grep' in cmd_l or 'rg ' in cmd_l or 'claude_monitor.py' in cmd_l:
                continue
            if not any(re.search(p, cmd_l, re.IGNORECASE) for p in patterns):
                continue

            cwd = _get_proc_cwd(pid)
            processes.append({
                'pid': pid,
                'cmd': cmd,
                'cwd': cwd,
                'session_file': _find_session_file_from_fds(pid),
                'status': _detect_live_status(pid),
                'age_seconds': _get_age_seconds(pid),
                'runtime': _get_proc_runtime(pid),
            })
    except Exception:
        pass
    processes.sort(key=lambda p: p.get('age_seconds', 0))
    return processes


def _merge_live_processes(sessions: List[Dict[str, Any]], live_processes: List[Dict[str, Any]]) -> None:
    if not live_processes:
        return

    used_session_ids: set[str] = set()
    sessions_by_file = {s.get('file'): s for s in sessions if s.get('file')}

    for proc in live_processes:
        target = None
        session_file = proc.get('session_file')
        if session_file and session_file in sessions_by_file:
            target = sessions_by_file[session_file]
        else:
            target = _match_session_by_cwd(sessions, proc, used_session_ids)

        if target:
            _apply_live_status(target, proc)
            used_session_ids.add(target.get('session_id', ''))
            continue

        session_id = Path(session_file).stem if session_file else f"claude-live-{proc['pid']}"
        project_slug = _cwd_to_project_slug(proc.get('cwd', ''))
        sessions.append({
            'session_id': session_id,
            'slug': session_id[:20],
            'title': '',
            'project': os.path.basename(proc.get('cwd') or '') or 'Claude Workspace',
            'project_slug': project_slug,
            'cwd': proc.get('cwd') or '未知目录',
            'git_branch': 'main',
            'status': proc.get('status') or STATUS_STANDBY,
            'last_tool': None,
            'age_seconds': proc.get('age_seconds', 0),
            'runtime': proc.get('runtime', '?'),
            'file': session_file or '',
            'is_live': True,
            'pid': proc['pid'],
        })
        used_session_ids.add(session_id)


def _match_session_by_cwd(sessions: List[Dict[str, Any]], proc: Dict[str, Any], used_session_ids: set[str]) -> Dict[str, Any] | None:
    cwd = proc.get('cwd') or ''
    project_slug = _cwd_to_project_slug(cwd)
    candidates = [
        s for s in sessions
        if s.get('session_id') not in used_session_ids
        and (
            (cwd and s.get('cwd') == cwd)
            or (project_slug and s.get('project_slug') == project_slug)
        )
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.get('age_seconds', 0))
    return candidates[0]


def _apply_live_status(session: Dict[str, Any], proc: Dict[str, Any]) -> None:
    session['is_live'] = True
    session['pid'] = proc['pid']
    session['runtime'] = proc.get('runtime', session.get('runtime', '?'))
    if proc.get('cwd') and session.get('cwd') in ['', '未知目录']:
        session['cwd'] = proc['cwd']
    if session.get('status') in [STATUS_IDLE, STATUS_DONE]:
        session['status'] = proc.get('status') or STATUS_STANDBY


def _find_session_file_from_fds(pid: str) -> str | None:
    fd_dir = Path(f'/proc/{pid}/fd')
    if not fd_dir.exists():
        return None
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except Exception:
                continue
            if not target.endswith('.jsonl'):
                continue
            if str(CLAUDE_DIR) in target:
                return target
    except Exception:
        pass
    return None


def _detect_live_status(pid: str) -> str:
    try:
        def _get_cpu_ticks(proc_id: str) -> int:
            with open(f'/proc/{proc_id}/stat') as f:
                stat = f.read().split()
            return int(stat[13]) + int(stat[14])

        t1 = _get_cpu_ticks(pid)
        time.sleep(0.1)
        t2 = _get_cpu_ticks(pid)
        if t2 > t1:
            return STATUS_EXECUTING
    except Exception:
        pass
    return STATUS_STANDBY


def _infer_status(events: list, age_seconds: int):
    """从最近事件推断状态"""
    last_tool = None
    cwd = None
    git_branch = None

    # 提取 cwd / git_branch
    for ev in events:
        if ev.get('cwd') and not cwd:
            cwd = ev['cwd']
        if ev.get('git_branch') and not git_branch:
            git_branch = ev['git_branch']

    # 找最后一条 assistant 消息
    last_assistant = None
    for ev in reversed(events):
        if ev.get('type') == 'assistant':
            last_assistant = ev
            break

    if last_assistant is None:
        return STATUS_IDLE, None, cwd, git_branch

    stop_reason = last_assistant.get('message', {}).get('stop_reason')
    content = last_assistant.get('message', {}).get('content', [])

    # 提取最后使用的工具名
    for block in reversed(content) if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get('type') == 'tool_use':
            last_tool = block.get('name')
            break

    # 推断状态
    if stop_reason is None and age_seconds < 30:
        return STATUS_THINKING, last_tool, cwd, git_branch
    if stop_reason == 'tool_use':
        if age_seconds < 90:
            return STATUS_EXECUTING, last_tool, cwd, git_branch
        return STATUS_IDLE, last_tool, cwd, git_branch
    if stop_reason == 'end_turn':
        return STATUS_IDLE, last_tool, cwd, git_branch

    # 检查 hook 事件中是否有 waiting_permission
    hook_status = _check_hook_events(events)
    if hook_status:
        return hook_status, last_tool, cwd, git_branch

    return STATUS_IDLE, last_tool, cwd, git_branch


def _check_hook_events(events: list) -> str | None:
    """检查是否有 waiting_permission hook 事件"""
    hook_file = Path('/tmp/claude_island_events.jsonl')
    if not hook_file.exists():
        return None
    try:
        lines = _tail_lines(hook_file, 20)
        now = time.time()
        for line in reversed(lines):
            try:
                ev = json.loads(line)
                if ev.get('event') == 'waiting_permission':
                    ts = ev.get('timestamp', 0)
                    if now - ts < 120:
                        return STATUS_WAITING
            except Exception:
                pass
    except Exception:
        pass
    return None


def _tail_lines(filepath: Path, n: int) -> list:
    """读取文件最后 n 行"""
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()
            buf = b''
            lines_found = 0
            pos = size
            while pos > 0 and lines_found <= n:
                chunk = min(2048, pos)
                pos -= chunk
                f.seek(pos)
                data = f.read(chunk)
                buf = data + buf
                lines_found = buf.count(b'\n')
            return buf.decode('utf-8', errors='replace').splitlines()[-n:]
    except Exception:
        return []


def _head_lines(filepath: Path, n: int) -> list:
    try:
        with open(filepath, 'r', errors='replace') as f:
            return [f.readline() for _ in range(n)]
    except Exception:
        return []


def _slug_to_project(slug: str) -> str:
    """将 project slug（URL 编码目录名）转换为可读项目名"""
    try:
        from urllib.parse import unquote
        return unquote(slug).replace('-', '/').split('/')[-1] or slug
    except Exception:
        return slug


def _cwd_to_project_slug(cwd: str) -> str:
    if not cwd:
        return ''
    return cwd.replace('/', '-')


def _get_age_seconds(pid: str) -> int:
    try:
        with open(f'/proc/{pid}/stat') as f:
            stat = f.read().split()
        ticks = int(stat[21])
        hz = os.sysconf('SC_CLK_TCK')
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
    runtime_s = _get_age_seconds(pid)
    if runtime_s < 60:
        return f'{runtime_s}s'
    if runtime_s < 3600:
        return f'{runtime_s // 60}m'
    return f'{runtime_s // 3600}h'


def _get_system_uptime() -> float:
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


def get_summary() -> Dict[str, Any]:
    """返回所有实例的汇总统计"""
    sessions = get_all_sessions()
    live_total = sum(1 for s in sessions if s.get('is_live'))
    thinking  = sum(1 for s in sessions if s['status'] == STATUS_THINKING)
    executing = sum(1 for s in sessions if s['status'] == STATUS_EXECUTING)
    waiting   = sum(1 for s in sessions if s['status'] == STATUS_WAITING)
    idle      = sum(1 for s in sessions if s['status'] in [STATUS_IDLE, STATUS_STANDBY])

    return {
        'total':      live_total,
        'thinking':   thinking,
        'executing':  executing,
        'permission': waiting,
        'idle':       idle,
        'sessions':   sessions,
    }


if __name__ == '__main__':
    # 命令行测试
    summary = get_summary()
    print(f"Claude Code 实例: {summary['total']} 个")
    for s in summary['sessions']:
        print(f"  [{s['status'][:4]}] {s['slug']:<30} {s['project']} ·{s['age_seconds']}s前")
