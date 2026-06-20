#!/usr/bin/env python3
"""
Agents Island — Windows 侧壳（本机 Python 3.11 + pywebview 6.1 + WebView2）
===========================================================================
无边框 / 透明 / 置顶，停靠屏幕顶部居中。窗口尺寸随岛 UI 四态缩放
（缩入时仅留 14px 触发条，最大限度不遮挡屏幕点击）。

UI 与数据全部来自 WSL 桥（http://127.0.0.1:5599/），本文件只负责原生窗口。

全局热键（岛未聚焦也可审批最早一条待审）：
  Ctrl+Alt+A = Allow   Ctrl+Alt+D = Deny   Ctrl+Alt+S = Always   Ctrl+Alt+Q = 退出

启动（通常由 launch/AgentsIsland.vbs 连带 WSL 桥一起拉起）：
  python win\\island.py
"""
import argparse
import ctypes
import ctypes.wintypes
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

# 必须在 WebView2 环境创建前设置：禁用 Chromium 对后台/被遮挡页面的
# 定时器节流与渲染器冻结 —— layered 透明窗会被原生遮挡计算误判为不可见，
# 空闲几分钟后页面被整体睡眠（JS 停摆、evaluate_js 全部挂起）。2026-06-11 实锤。
os.environ.setdefault(
    'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
    '--disable-background-timer-throttling '
    '--disable-backgrounding-occluded-windows '
    '--disable-renderer-backgrounding '
    '--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion '
    '--autoplay-policy=no-user-gesture-required')

import webview


def eval_js_timeout(win, script, timeout=3.0):
    """带超时的 evaluate_js：渲染器假死时绝不挂住调用线程。
    超时返回 '__timeout__'（泄漏的工作线程在页面复活后自然退出）。"""
    box = {}

    def _run():
        try:
            box['v'] = win.evaluate_js(script)
        except Exception:
            box['v'] = None

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get('v', '__timeout__') if not t.is_alive() else '__timeout__'


def eval_js_nowait(win, script):
    """fire-and-forget：托盘菜单/热键用，永不阻塞调用线程。"""
    threading.Thread(target=lambda: eval_js_timeout(win, script, 5.0),
                     daemon=True).start()


def toggle_mute():
    def _post():
        try:
            req = urllib.request.Request(f'{BRIDGE}/api/mute', data=b'{}', method='POST')
            urllib.request.urlopen(req, timeout=1.5).read()
        except OSError:
            pass
    threading.Thread(target=_post, daemon=True).start()


TRAY_MENU_I18N = {
    'zh': ['🏝  展开 / 收起面板 Ctrl+Alt+E', '🔕  勿扰开 / 关 Ctrl+Alt+M',
           '⏱  审批超时自动放行(25s) 开/关', '↻  重载页面', '✕  退出 Ctrl+Alt+Q'],
    'en': ['🏝  Toggle panel Ctrl+Alt+E', '🔕  Do-not-disturb Ctrl+Alt+M',
           '⏱  Auto-allow on timeout (25s)', '↻  Reload page', '✕  Quit Ctrl+Alt+Q'],
}


def menu_lang() -> str:
    """托盘菜单语言：桥设置 lang 优先，缺省跟系统区域（中文区→zh）。"""
    try:
        with urllib.request.urlopen(f'{BRIDGE}/api/state', timeout=2) as r:
            lang = json.loads(r.read()).get('lang', '')
        if lang in TRAY_MENU_I18N:
            return lang
    except OSError:
        pass
    try:
        import locale
        loc = (locale.getlocale()[0] or '').lower()
        return 'zh' if loc.startswith(('zh', 'chinese')) else 'en'
    except Exception:
        return 'zh'


def toggle_auto_allow():
    """超时自动放行（25s）开/关：读当前值取反后写回桥设置。"""
    def _post():
        try:
            with urllib.request.urlopen(f'{BRIDGE}/api/state', timeout=1.5) as r:
                cur = json.loads(r.read()).get('auto_allow_timeout', 0)
            body = json.dumps({'auto_allow_timeout': 0 if cur else 25}).encode()
            req = urllib.request.Request(f'{BRIDGE}/api/settings', data=body, method='POST')
            urllib.request.urlopen(req, timeout=1.5).read()
        except OSError:
            pass
    threading.Thread(target=_post, daemon=True).start()


def hotkey_decide(action: str):
    """全局审批热键：直发桥即时决策最旧 pending（零轮询延迟，根治"按3次"）。"""
    def _post():
        try:
            req = urllib.request.Request(
                f'{BRIDGE}/api/hotkey',
                data=json.dumps({'action': action}).encode(), method='POST')
            urllib.request.urlopen(req, timeout=1.5).read()
        except OSError:
            pass
    threading.Thread(target=_post, daemon=True).start()


def bridge_event(payload: dict):
    """Python→页面事件一律经桥中转（页面随轮询取走）。
    evaluate_js 会被 pywebview 串行锁堵死（2026-06-11 实锤），不再用于推送。"""
    def _post():
        try:
            req = urllib.request.Request(
                f'{BRIDGE}/api/ui_event',
                data=json.dumps(payload).encode(), method='POST')
            urllib.request.urlopen(req, timeout=1.5).read()
        except OSError:
            pass
    threading.Thread(target=_post, daemon=True).start()

FROZEN = bool(getattr(sys, 'frozen', False))
RES_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))  # 只读资源
DATA_DIR = Path.home() / '.agents-island'    # frozen 可写区（日志/pid/设置）
if FROZEN:
    DATA_DIR.mkdir(exist_ok=True)
LOG = (DATA_DIR / 'island_win.log') if FROZEN else Path(__file__).with_name('island_win.log')
CONFIG_FILE = RES_DIR / 'island_config.json'
DEFAULTS = {
    'bridge_port': 5599,
    'poll_ms': 1000,
    'hotkeys': True,
    'top_margin': 0,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


CFG = load_config()
BRIDGE = f"http://127.0.0.1:{CFG['bridge_port']}"

# 四态窗口几何（含投影留白；与 island.css 各态尺寸对应）
# sliver 仅 14px 高的触发条——缩入态几乎不遮挡屏幕
# 窗口 = 岛体 CSS 精确尺寸（SetWindowRgn 真异形窗，无透明边距）
GEOM = {
    'sliver':   (220, 6),
    'compact':  (320, 37),
    'approval': (432, 118),   # ask 选项多时由 JS 传内容高覆盖
    'expanded': (478, 480),   # 高度由 JS 传入的内容高度覆盖
    'menu':     (248, 256),   # 托盘右键 HTML 玻璃菜单；高度由 JS 传内容高覆盖
}
# 各态圆角（CSS px，物理化后喂给 CreateRoundRectRgn）
RADIUS = {'sliver': 3, 'compact': -1, 'approval': 26, 'expanded': 30, 'menu': 20}  # -1=全胶囊(h/2)


# （FROZEN/RES_DIR/DATA_DIR/LOG 已前移至 CONFIG_FILE 之前）


def _log(msg: str):
    line = f'{time.strftime("%H:%M:%S")} {msg}'
    print(line, flush=True)
    try:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


# webview.start() 后 pywebview 切换 DPI 感知，screens 读数会从逻辑变物理；
# 启动时缓存一次，全程用同一坐标系（与 create_window 的 x 同系）
SCREEN_W = None


def screen_width() -> int:
    global SCREEN_W
    if SCREEN_W is None:
        try:
            SCREEN_W = webview.screens[0].width
        except Exception:
            SCREEN_W = ctypes.windll.user32.GetSystemMetrics(0)
    return SCREEN_W


class IslandApi:
    """暴露给 island.js 的原生窗口协调接口。"""

    def __init__(self):
        self._window = None   # 下划线开头：pywebview js_api 桥不得序列化 Window 对象（含 native Form 无限属性链，会递归爆栈）
        self._working = 0     # JS 推送的 working agent 数（驱动托盘动画）

    def set_working(self, n) -> bool:
        """island.js 在 working 数变化时调用；>0 时托盘 logo 公转。"""
        try:
            self._working = int(n)
        except (TypeError, ValueError):
            self._working = 0
        _log(f'working={self._working}')
        return True

    def _hwnd(self):
        return int(str(self._window.native.Handle))

    def resize_for(self, mode: str, content_h: int = 0) -> bool:
        """Win32 SetWindowPos 直接以物理像素定位。
        弃用 pywebview resize/move：其 move 乘 DPI 倍率而 resize 不乘，
        200% 缩放屏上窗口只剩一半 CSS 宽度，岛体被裁。"""
        if self._window is None or mode not in GEOM:
            return False
        w, h = GEOM[mode]
        if mode in ('expanded', 'approval', 'menu') and content_h:
            h = int(content_h)             # 窗口=内容精确高
        try:
            hwnd = self._hwnd()
            user32 = ctypes.windll.user32
            scale = user32.GetDpiForWindow(hwnd) / 96.0
            pw, ph = int(w * scale), int(h * scale)
            sw = user32.GetSystemMetrics(0)            # 物理屏宽（进程已 DPI aware）
            x = (sw - pw) // 2
            y = int(CFG['top_margin'] * scale)
            # 托盘菜单：定位到右键光标左上方（Windows 右键直觉），而非岛的顶部中央
            anchor = getattr(self, '_menu_anchor', None)
            if mode == 'menu' and anchor:
                sh = user32.GetSystemMetrics(1)
                ax, ay = anchor
                x = max(8, min(ax - pw + 12, sw - pw - 8))   # 菜单右缘≈光标，钳右边界
                y = max(8, min(ay - ph + 12, sh - ph - 8))   # 菜单下缘≈光标，钳下边界
            # resize 只管几何，z 序交给 WinForms TopMost 属性。
            if mode != 'sliver':
                user32.SetWindowRgn(hwnd, None, True)   # 先清旧 Region，生长动画不被裁
            rect0 = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect0))
            x0, y0 = rect0.left, rect0.top
            w0, h0 = rect0.right - rect0.left, rect0.bottom - rect0.top
            ctypes.set_last_error(0)
            ok = 1
            # 涉及菜单的转换都一步到位：菜单在光标处(右下)，逐帧生长会横跨屏幕
            # 飞行——进入(→menu)与离开(menu→)都瞬移 + CSS 淡入
            one_step = (mode == 'menu') or (getattr(self, '_last_mode', None) == 'menu')
            self._last_mode = mode
            if one_step:
                ok = user32.SetWindowPos(hwnd, None, x, y, pw, ph, 0x0014)
            else:
                # 时间基驱动动画：按真实经过时间算进度，不靠固定步数×sleep。
                # SetWindowPos 重排耗时不定，固定 sleep 会让总时长漂移、帧距不匀；
                # 时间基让慢机自动少帧/快机多帧，总时长恒定 → 一致丝滑。
                GROW_DUR = 0.16
                t_start = time.perf_counter()
                while True:
                    t = (time.perf_counter() - t_start) / GROW_DUR
                    if t >= 1.0:
                        t = 1.0
                    e = 1 - (1 - t) ** 3                # ease-out cubic
                    cx_ = int(x0 + (x - x0) * e)
                    cy_ = int(y0 + (y - y0) * e)
                    cw_ = int(w0 + (pw - w0) * e)
                    ch_ = int(h0 + (ph - h0) * e)
                    ok = user32.SetWindowPos(hwnd, None, cx_, cy_, cw_, ch_, 0x0014)
                    if t >= 1.0:
                        break
                    time.sleep(0.008)                   # 让出 CPU，下帧按真实时间定位
            err = ctypes.get_last_error()
            # 圆角策略：Win11 DWM 原生圆角（系统级抗锯齿，平滑）；
            # SetWindowRgn 是无 AA 的硬像素裁剪（边缘锯齿），只用于 sliver 细条
            # ——顺带裁掉 WinForms 最小窗高钳制出的多余黑边。
            try:
                pref = ctypes.c_int(1 if mode == 'sliver' else 2)  # sliver 不让 DWM 画圆角框
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
            except Exception:
                pass
            if mode == 'sliver':
                pr = int(6 * scale)
                vis_h = int(6 * scale)                  # 可见高度 = 6 CSS px
                rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, pw + 1, vis_h + 1, pr, pr)
                user32.SetWindowRgn(hwnd, rgn, True)
            else:
                user32.SetWindowRgn(hwnd, None, True)   # 清 Region → DWM 圆角接管
            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            _log(f'resize_for {mode} css=({w}x{h}) want_phys=({pw}x{ph}@{x}) ok={ok} err={err} '
                 f'actual=({rect.left},{rect.top},{rect.right - rect.left}x{rect.bottom - rect.top})')
            return True
        except Exception as e:
            _log(f'resize_for {mode} fallback ({type(e).__name__}: {e})')
            self._window.resize(w, h)
            self._window.move((screen_width() - w) // 2, CFG['top_margin'])
            return True

    GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000

    def jump_to(self, info) -> str:
        """双击会话行：聚焦该会话所在终端窗口；找不到则 wt 新开 claude --resume。
        Windows 无法像 macOS 用 TTY 精确定位，v1=窗口标题模糊匹配（Claude Code
        会把会话标题写进终端标题）。"""
        try:
            title = str((info or {}).get('title') or '').strip()
            agent = str((info or {}).get('agent') or 'claude')
            sid   = str((info or {}).get('session_id') or '')
            cwd   = str((info or {}).get('cwd') or '~')
            user32 = ctypes.windll.user32

            # SSH 远程会话：本机无窗口可聚焦，wt 新开 ssh 终端尝试 resume
            remote_ssh = str((info or {}).get('remote_ssh') or '').strip()
            if (info or {}).get('remote') and remote_ssh:
                inner = f"cd {cwd} && claude --resume {sid} || exec $SHELL" \
                    if agent == 'claude' and sid else f"cd {cwd}; exec $SHELL"
                # remote_ssh 配置串建议含 -t（如 "ssh -t -p 2222 user@host"），
                # host 之后只能跟远端命令
                cmd = ['wt.exe', 'nt'] + remote_ssh.split() + [inner]
                import subprocess
                subprocess.Popen(cmd, creationflags=0x08000000)
                _log(f'jump_to: remote ssh terminal ({remote_ssh})')
                return 'remote-ssh'

            # tmux pane 级精确跳转：桥在 WSL 侧按 cwd 定位并 switch 过去，
            # 此处再聚焦宿主终端窗口（优先匹配含 tmux 会话名的标题）。
            # WT tab 级为平台上限外：无 tab 枚举/外部聚焦 API（详 bridge._tmux_locate）
            tmux_sess = ''
            try:
                body = json.dumps({'cwd': cwd, 'session_id': sid}).encode()
                req = urllib.request.Request(f'{BRIDGE}/api/jump_assist',
                                             data=body, method='POST')
                with urllib.request.urlopen(req, timeout=3) as r:
                    j = json.loads(r.read())
                if j.get('tmux'):
                    tmux_sess = str(j.get('session_name') or '')
                    _log(f'jump_to: tmux pane switched ({tmux_sess})')
            except OSError:
                pass

            target = {'hwnd': 0}
            needles = []
            if tmux_sess:
                needles += [tmux_sess.lower(), 'tmux']
            if title:
                needles.append(title[:24].lower())
            if needles:
                @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
                def _enum(hwnd, _l):
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, buf, 256)
                    t = buf.value.lower() if buf.value else ''
                    if t and buf.value != 'Agents Island' and any(n in t for n in needles):
                        target['hwnd'] = hwnd
                        return False
                    return True
                user32.EnumWindows(_enum, 0)

            if target['hwnd']:
                user32.ShowWindow(target['hwnd'], 9)          # SW_RESTORE
                # ALT 键解锁前台限制（经典技巧）；岛通常已是前台进程，双保险
                user32.keybd_event(0x12, 0, 0, 0)
                user32.SetForegroundWindow(target['hwnd'])
                user32.keybd_event(0x12, 0, 0x2, 0)           # KEYEVENTF_KEYUP
                _log(f'jump_to: focused window for "{title[:20]}"')
                return 'focused'

            # 兜底：wt 新开终端恢复会话（仅 claude 可 --resume；其余开到 cwd）
            distro_file = Path(__file__).resolve().parent.parent / 'launch' / 'distro.txt'
            distro = []
            if distro_file.exists():
                d = distro_file.read_text(encoding='utf-8').strip().splitlines()[0].strip()
                if d:
                    distro = ['-d', d]
            if agent == 'claude' and sid:
                cmd = ['wt.exe', 'nt', 'wsl.exe', *distro, '--cd', cwd, '--', 'claude', '--resume', sid]
            else:
                cmd = ['wt.exe', 'nt', 'wsl.exe', *distro, '--cd', cwd]
            import subprocess
            subprocess.Popen(cmd, creationflags=0x08)          # DETACHED_PROCESS
            _log(f'jump_to: spawned terminal ({agent}, resume={bool(sid) and agent=="claude"})')
            return 'spawned'
        except Exception as e:
            _log(f'jump_to failed: {type(e).__name__}: {e}')
            return 'error'

    def assert_topmost(self) -> bool:
        """强制窗口 TOPMOST（HWND_TOPMOST=-1，SWP_NOMOVE|NOSIZE|NOACTIVATE）。
        周期自愈 + 弹出时调用：把岛抬回置顶层最前，不抢键盘焦点。
        修复「掉出置顶层 / 被后激活的其他 topmost 窗口盖住」——beep 响却看不见窗口的根因。"""
        try:
            hwnd = self._hwnd()
            ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            return True
        except Exception:
            return False

    def surface_alert(self) -> bool:
        """通知/审批弹出时调用：① 重申置顶到最前；② 任务栏闪烁兜底——
        Windows 前台锁常让后台进程抢不到前台，闪烁可在不抢焦点的前提下引起注意
        （独占全屏程序仍无解，那时以声音为准）。"""
        ok = self.assert_topmost()
        try:
            hwnd = self._hwnd()

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [('cbSize', ctypes.wintypes.UINT),
                            ('hwnd', ctypes.wintypes.HWND),
                            ('dwFlags', ctypes.wintypes.DWORD),
                            ('uCount', ctypes.wintypes.UINT),
                            ('dwTimeout', ctypes.wintypes.DWORD)]
            FLASHW_ALL, FLASHW_TIMERNOFG = 0x00000003, 0x0000000C
            fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                            FLASHW_ALL | FLASHW_TIMERNOFG, 3, 0)
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
        except Exception:
            pass
        return ok

    def _autostart_lnk(self):
        """开机自启快捷方式路径（shell:startup 文件夹内）。"""
        startup = os.path.join(os.environ.get('APPDATA', ''),
                               r'Microsoft\Windows\Start Menu\Programs\Startup')
        return os.path.join(startup, 'Agents Island.lnk')

    def is_autostart(self) -> bool:
        """是否已开机自启（startup 文件夹存在本应用快捷方式）。"""
        try:
            return os.path.exists(self._autostart_lnk())
        except Exception:
            return False

    def set_autostart(self, on) -> bool:
        """开/关开机自启：在 shell:startup 建/删指向启动器的快捷方式。
        打包态指向 exe 自身；开发态指向 launch/AgentsIsland.vbs（与桌面图标一致）。
        返回最终状态（供菜单刷新）。"""
        import subprocess
        lnk = self._autostart_lnk()
        try:
            if on:
                if FROZEN:
                    target = sys.executable
                    workdir = os.path.dirname(sys.executable)
                else:
                    app_root = Path(__file__).resolve().parent.parent
                    target = str(app_root / 'launch' / 'AgentsIsland.vbs')
                    workdir = str(app_root / 'launch')
                icon = str(RES_DIR / 'island.ico')
                ps = ("$s=New-Object -ComObject WScript.Shell;"
                      f"$l=$s.CreateShortcut('{lnk}');"
                      f"$l.TargetPath='{target}';$l.WorkingDirectory='{workdir}';"
                      f"$l.IconLocation='{icon}';$l.Description='Agents Island';$l.Save()")
                subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               creationflags=0x08000000, timeout=12)
            elif os.path.exists(lnk):
                os.remove(lnk)
            _log(f'set_autostart({on}) -> {self.is_autostart()}')
        except Exception as e:
            _log(f'set_autostart {on}: {type(e).__name__}: {e}')
        return self.is_autostart()

    def tray_action(self, name) -> bool:
        """HTML 玻璃菜单里需原生能力的项（reload/quit）回调到这里。"""
        try:
            if name == 'reload':
                # load_url 到同一 URL 在 WebView2 可能被判为 no-op（毫无反馈），
                # 故优先用 location.reload() 强制真重载；失败再退回 load_url。
                try:
                    self._window.evaluate_js('location.reload()')
                except Exception:
                    self._window.load_url(f"{BRIDGE}/?poll={CFG['poll_ms']}")
            elif name == 'quit':
                tray = getattr(self, '_tray', None)
                if tray is not None:
                    tray.Visible = False
                    tray.Dispose()
                self._window.destroy()
            return True
        except Exception as e:
            _log(f'tray_action {name}: {type(e).__name__}: {e}')
            return False

    def set_interactive(self, on) -> bool:
        """交互态开关：on=摘除 WS_EX_NOACTIVATE，让 WebView2 收到鼠标点击
        （focus=False 的层叠透明窗默认吞掉点击，按钮/展开点击全失效）。
        sliver/compact 被动态关掉，避免 hover 时抢占前台焦点。"""
        try:
            hwnd = self._hwnd()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            if on:
                user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style & ~self.WS_EX_NOACTIVATE)
                user32.SetForegroundWindow(hwnd)   # 激活 → 保证 WebView2 收到鼠标点击
            else:
                user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style | self.WS_EX_NOACTIVATE)
            return True
        except Exception as e:
            _log(f'set_interactive failed: {e}')
            return False

    def focus_input(self) -> bool:
        """岛上作答输入框需要键盘焦点：临时摘掉 WS_EX_NOACTIVATE 并前置窗口。"""
        try:
            hwnd = self._hwnd()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style & ~self.WS_EX_NOACTIVATE)
            user32.SetForegroundWindow(hwnd)
            _log('focus_input: NOACTIVATE off')
            return True
        except Exception as e:
            _log(f'focus_input failed: {e}')
            return False

    def unfocus_input(self) -> bool:
        """作答完毕恢复不抢焦点属性。"""
        try:
            hwnd = self._hwnd()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style | self.WS_EX_NOACTIVATE)
            return True
        except Exception:
            return False

    def apply_transparency_key(self):
        """Form 底色=TransparencyKey=#010101：CSS 透明处露出该色被系统抠除
        （含点击穿透）。岛体纯黑 #000000 不在键色上，不受影响。
        pywebview transparent 模式只透 WebView2 表面，顶层 Form 仍是灰白底，
        必须配合本键色才能得到真·异形窗。
        同时隐藏任务栏按钮（岛是常驻 overlay，不该占任务栏位）。"""
        try:
            import System
            from System.Drawing import Color
            form = self._window.native

            def _apply():
                form.BackColor = Color.Black   # Region 裁剪窗：底色=岛色，无键色无层叠
                form.ShowInTaskbar = False
                form.TopMost = True            # WinForms 属性级置顶（裸 SetWindowPos 会被 WinForms 覆盖）
            form.Invoke(System.Action(_apply))
            try:
                pref = ctypes.c_int(2)   # DWMWCP_ROUND：系统级平滑圆角
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    self._hwnd(), 33, ctypes.byref(pref), 4)
                none_ = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE：去窗口边框线
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    self._hwnd(), 34, ctypes.byref(none_), 4)
            except Exception:
                pass                     # Win10 无此属性：方角降级
            _log('window chrome applied (DWM round corners, TopMost=True)')
        except Exception as e:
            _log(f'window chrome failed: {type(e).__name__}: {e}')

    def setup_tray(self):
        """系统托盘常驻图标：左键/菜单控制面板，退出走托盘。"""
        try:
            import System
            from System.Drawing import Icon
            import System.Windows.Forms as WF
            form = self._window.native
            ico_path = str(RES_DIR / 'island.ico')

            def _load(subdir, pat):
                d = RES_DIR / subdir
                return [Icon(str(fp)) for fp in sorted(d.glob(pat))] if d.exists() else []

            def _build():
                form.Icon = Icon(ico_path)          # 窗口/Alt-Tab 图标
                tray = WF.NotifyIcon()
                tray.Icon = Icon(ico_path)
                tray.Text = 'Agents Island'
                # 托盘三态帧集：睡觉(4)/悬浮舞(9)/爆发(6)，随 working 数切换
                active_frames = _load('tray_frames', 'f*.ico')
                sleep_frames = _load('tray_sleep', 's*.ico')
                super_frames = _load('tray_super', 's*.ico')
                idle_fp = RES_DIR / 'tray_idle.ico'
                idle_icon = Icon(str(idle_fp)) if idle_fp.exists() else Icon(ico_path)
                tray.Icon = sleep_frames[0] if sleep_frames else idle_icon
                # state: i=当前帧, set=当前帧集标识, tick=分频计数（睡觉降速）
                state = {'i': 0, 'set': '', 'tick': 0}
                if active_frames or sleep_frames or super_frames:
                    timer = WF.Timer()
                    timer.Interval = 150

                    def _tick(s, e):
                        w = self._working
                        if w > 2 and super_frames:
                            cur, name, div = super_frames, 'super', 1      # 爆发：每 tick 进帧（快）
                        elif w >= 1 and active_frames:
                            cur, name, div = active_frames, 'active', 1
                        elif sleep_frames:
                            cur, name, div = sleep_frames, 'sleep', 3      # 睡觉：每 3 tick 进帧（慢）
                        else:
                            cur, name, div = ([idle_icon], 'idle', 1)
                        state['tick'] += 1
                        if name != state['set']:
                            state['set'] = name; state['i'] = 0; state['tick'] = 0
                        elif state['tick'] % div == 0:
                            state['i'] = (state['i'] + 1) % len(cur)
                        tray.Icon = cur[state['i'] % len(cur)]
                    timer.Tick += _tick
                    timer.Start()
                    self._tray_timer = timer        # 保引用防 GC
                def _act(action):
                    def h(s, e):
                        bridge_event({'type': 'action', 'action': action})
                    return h

                # 托盘右键 → 岛弹 HTML 玻璃菜单（弃 WinForms ContextMenuStrip：
                # 渲染天花板低、自定义 Renderer 子类化会 AccessViolation 崩进程，
                # 06-11 实锤）。左键双击仍唤岛。
                def _on_mouseup(s, e):
                    if e.Button == WF.MouseButtons.Right:
                        # 记录右键光标物理坐标，菜单定位到此处（Windows 右键直觉）
                        pt = ctypes.wintypes.POINT()
                        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                        self._menu_anchor = (pt.x, pt.y)
                        bridge_event({'type': 'action', 'action': 'menu'})
                tray.MouseUp += _on_mouseup
                tray.DoubleClick += _act('toggle')
                tray.Visible = True
                self._tray = tray            # 保引用防 GC

            form.Invoke(System.Action(_build))
            _log('tray icon ready')
        except Exception as e:
            _log(f'tray setup failed: {type(e).__name__}: {e}')

    def quit(self):
        tray = getattr(self, '_tray', None)
        if tray:
            try:
                tray.Visible = False
                tray.Dispose()
            except Exception:
                pass
        if self._window:
            self._window.destroy()


def wait_bridge(timeout: float = 60.0) -> bool:
    """等待 WSL 桥就绪（launcher 先拉桥，这里容忍冷启动时差）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'{BRIDGE}/api/health', timeout=2):
                return True
        except OSError:
            time.sleep(1.0)
    return False


# ── 全局热键（RegisterHotKey + 消息循环线程） ─────────────────────────
HOTKEYS = {1: ('A', 'allow'),
           2: ('D', 'deny'),
           3: ('S', 'always'),
           4: ('Q', None),        # Q = 退出（本地处理）
           5: ('E', 'toggle'),    # E = 开关面板
           6: ('M', 'mute')}      # M = 勿扰切换
MOD_ALT, MOD_CONTROL, WM_HOTKEY = 0x0001, 0x0002, 0x0312


def hotkey_loop(api: IslandApi):
    user32 = ctypes.windll.user32
    registered = []
    for hk_id, (key, _js) in HOTKEYS.items():
        if user32.RegisterHotKey(None, hk_id, MOD_CONTROL | MOD_ALT, ord(key)):
            registered.append(hk_id)
    if not registered:
        return                                  # 全部被占用：岛内按键仍可用，非致命
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
            action = HOTKEYS[msg.wParam][1]
            try:
                if action is None:
                    api.quit()
                    break
                elif action == 'toggle':
                    bridge_event({'type': 'action', 'action': 'toggle'})
                elif action == 'mute':
                    toggle_mute()
                else:
                    hotkey_decide(action)      # allow/deny/always：桥侧即时决策
            except Exception:
                pass
    for hk_id in registered:
        user32.UnregisterHotKey(None, hk_id)


def _style_tray_menu(menu):
    """托盘菜单深色化（与岛视觉一致）：纯属性配色 + DWM 圆角弹出。
    ⚠️ 禁用 pythonnet 子类化 ToolStripProfessionalRenderer——在 pywebview
    的 pythonnet 环境里类派生直接 AccessViolation 崩进程（2026-06-11 实锤，
    曾连续带崩 3 个实例）。hover 高亮保留系统色，深底上对比可接受。
    任何一段失败只降级该段，绝不影响托盘本体构建。"""
    import System.Windows.Forms as WF
    from System.Drawing import Color, Font
    try:
        menu.ShowImageMargin = False
        menu.BackColor = Color.FromArgb(255, 17, 17, 22)
        menu.ForeColor = Color.FromArgb(255, 233, 233, 236)
        menu.Font = Font('Segoe UI', 9.75)
        menu.RenderMode = WF.ToolStripRenderMode.System   # 平面渲染，尊重 BackColor
    except Exception as exc:
        _log(f'tray menu base colors failed: {type(exc).__name__}: {exc}')
    try:
        for it in menu.Items:
            it.Padding = WF.Padding(2, 5, 2, 5)
    except Exception as exc:
        _log(f'tray menu padding failed: {type(exc).__name__}: {exc}')

    def _round_popup(s, e):
        try:
            pref = ctypes.c_int(3)         # DWMWCP_ROUNDSMALL
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(menu.Handle.ToInt64()), 33, ctypes.byref(pref), 4)
        except Exception:
            pass
    menu.Opening += _round_popup


PID_FILE = (DATA_DIR / 'island.pid') if FROZEN else Path(__file__).with_name('island.pid')


def _existing_page_alive() -> bool:
    """旧实例活体判定：桥心跳 client_age 健康 = 页面在拉 /api/state。
    桥不可达视为活（无法判定，宁可不杀）；client_age 异常连测两次防误判。"""
    for _ in range(2):
        try:
            with urllib.request.urlopen(
                    f"{BRIDGE}/api/health", timeout=2) as r:
                age = json.loads(r.read()).get('client_age', -1)
            if 0 <= age <= 45:
                return True
        except OSError:
            return True
        time.sleep(3)
    return False


def _kill_stale_instance():
    """按 pidfile 杀掉僵尸旧实例（校验进程名含 python，防 PID 复用误杀）。"""
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return
    if pid == os.getpid():
        return
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000 | 0x0001, False, pid)   # QUERY_LIMITED | TERMINATE
    if not h:
        return
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = ctypes.wintypes.DWORD(512)
        name = ''
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = buf.value.lower()
        if 'python' in os.path.basename(name):
            _log(f'taking over: killing stale island pid={pid}')
            k32.TerminateProcess(h, 1)
    finally:
        k32.CloseHandle(h)


def acquire_singleton() -> bool:
    """单实例互斥；旧实例页面僵死时接管（杀旧→重试拿锁）。"""
    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, 'AgentsIslandSingleton')
    if k32.GetLastError() != 183:                      # 拿到锁
        return True
    if _existing_page_alive():                          # 旧实例健康 → 静默退出
        return False
    k32.CloseHandle(handle)
    _kill_stale_instance()
    deadline = time.time() + 10
    while time.time() < deadline:
        time.sleep(1)
        handle = k32.CreateMutexW(None, False, 'AgentsIslandSingleton')
        if k32.GetLastError() != 183:
            return True
        k32.CloseHandle(handle)
    return False


def _self_restart():
    """WebView2 救不活时的最后一级自愈：原参重启本进程。
    先关互斥句柄（对象随之释放），继任实例才能拿到锁。"""
    import subprocess
    _log('escalating: WebView2 not recovering, restarting island process')
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                     creationflags=0x00000008 | 0x00000200 | 0x08000000)
    os._exit(1)   # 进程消亡 → 互斥对象释放；继任带 10s 重试窗口


def run_bridge_mode(extra=None):
    """frozen 打包态：本 exe 以 --bridge 参数运行时化身桥进程。
    桥代码与 web/ 静态资源随包（datas），__file__ 语义在解包目录内成立。"""
    bridge_py = RES_DIR / 'bridge' / 'island_bridge.py'
    sys.argv = [str(bridge_py)] + list(extra or [])
    import runpy
    runpy.run_path(str(bridge_py), run_name='__main__')


def ensure_bridge():
    """frozen：桥不在则 spawn 自身 --bridge 子进程（Windows 本机桥，
    本地 agent 扫描自然空转，纯聚合远程——同事零 WSL 依赖形态）。"""
    import subprocess
    import urllib.request as _u
    try:
        _u.urlopen(f'{BRIDGE}/api/health', timeout=1.5).read()
        return
    except OSError:
        pass
    subprocess.Popen([sys.executable, '--bridge'],
                     creationflags=0x08000000 | 0x00000200)   # NO_WINDOW
    _log('spawned embedded bridge subprocess')


def tunnel_keeper():
    """SSH 隧道托管（Windows ssh.exe）：settings.remotes[].tunnel =
    {"local":5598, "ssh":"-p 2222 user@host", "remote_port":5599}。
    端口活着不动；断了重拉。无配置则线程低频空转。"""
    import socket
    import subprocess
    settings_path = DATA_DIR / 'settings.json'
    while True:
        tunnels = []
        try:
            remotes = json.loads(settings_path.read_text(encoding='utf-8')).get('remotes', [])
            tunnels = [r['tunnel'] for r in remotes if isinstance(r.get('tunnel'), dict)]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        for t in tunnels:
            try:
                local = int(t.get('local', 0))
                if not local:
                    continue
                with socket.socket() as sk:
                    sk.settimeout(1)
                    if sk.connect_ex(('127.0.0.1', local)) == 0:
                        continue            # 端口已通
                args = ['ssh', '-N', '-L',
                        f"{local}:127.0.0.1:{int(t.get('remote_port', 5599))}",
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                        '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'BatchMode=yes'] + str(t.get('ssh', '')).split()
                subprocess.Popen(args, creationflags=0x08000000)
                _log(f'tunnel spawn: {local} <- {t.get("ssh", "")}')
            except (OSError, ValueError) as e:
                _log(f'tunnel error: {e}')
        time.sleep(20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--debug', action='store_true', help='开 WebView2 DevTools')
    ap.add_argument('--bridge', action='store_true', help='（打包态内部用）以桥进程运行')
    args, extra = ap.parse_known_args()

    if args.bridge:
        run_bridge_mode(extra)
        return

    # 单实例互斥：旧实例健康则静默退出；页面僵死则接管（杀旧拿锁）
    if not acquire_singleton():
        sys.exit(0)
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass

    if FROZEN:
        threading.Thread(target=tunnel_keeper, daemon=True).start()
        ensure_bridge()

    if not wait_bridge():
        ctypes.windll.user32.MessageBoxW(
            None,
            'WSL 桥未就绪（127.0.0.1:%d）。\n请先运行 launch/AgentsIsland.vbs 或手动启动 start_bridge.sh。'
            % CFG['bridge_port'],
            'Agents Island', 0x10)
        sys.exit(1)

    api = IslandApi()
    w, h = GEOM['sliver']
    window = webview.create_window(
        'Agents Island',
        url=f"{BRIDGE}/?poll={CFG['poll_ms']}",
        js_api=api,
        width=w, height=h,
        x=(screen_width() - w) // 2, y=CFG['top_margin'],
        frameless=True,
        on_top=True,
        transparent=False,   # Region 异形窗：页面黑底，无层叠透明（透明键=鼠标穿透元凶）
        easy_drag=False,
        focus=True,   # 必须 True：focus=False 时 pywebview 在 on_activated 反复强加 WS_EX_NOACTIVATE，WebView2 收不到任何鼠标输入（连 mousemove 都没有）
        shadow=False,
        min_size=(96, 5),   # 放开默认 200×100 下限
        background_color='#000000',
    )
    api._window = window

    if CFG['hotkeys']:
        threading.Thread(target=hotkey_loop, args=(api,), daemon=True).start()

    def cursor_watch(win):
        """权威 hover 信号：轮询全局光标是否在窗口矩形内，变化时推给 JS。
        原生窗口移动/缩放会让 Chrome 边界事件失灵（补发 leave 不补发 enter），
        导致 hover 中的岛被误收回。"""
        user32 = ctypes.windll.user32
        pt = ctypes.wintypes.POINT()
        rect = ctypes.wintypes.RECT()
        last = None
        menu_focused = False   # 菜单态是否曾拿到前台焦点（失焦消散判据）
        topmost_tick = 0       # 周期自愈置顶计数（每 8×0.25s≈2s 重申一次）
        time.sleep(6)
        hwnd = api._hwnd()
        while True:
            try:
                # 周期自愈置顶：每 ~2s 重申一次 HWND_TOPMOST，修复「掉出置顶层被普通/后激活窗口盖住」
                topmost_tick = (topmost_tick + 1) % 8
                if topmost_tick == 0:
                    api.assert_topmost()
                user32.GetCursorPos(ctypes.byref(pt))
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                inside = rect.left <= pt.x <= rect.right and rect.top <= pt.y <= rect.bottom
                if inside != last:
                    _log(f'cursor_watch inside={inside} pt=({pt.x},{pt.y}) rect=({rect.left},{rect.top},{rect.right},{rect.bottom})')
                    bridge_event({'type': 'cursor', 'inside': inside})
                    last = inside
                # 托盘菜单失焦即收：菜单弹出时 set_interactive 已把岛置前台；
                # 待观察到岛确为前台后，一旦焦点转到别的窗口（点了别的应用/桌面）→ 即收。
                # 先确认拿到焦点再判失焦，避开菜单刚开、前台尚未settle 的误收。
                if getattr(api, '_last_mode', None) == 'menu':
                    fg = user32.GetForegroundWindow()
                    if fg == hwnd:
                        menu_focused = True
                    elif menu_focused:
                        menu_focused = False
                        bridge_event({'type': 'action', 'action': 'menu_dismiss'})
                else:
                    menu_focused = False
            except Exception:
                pass
            time.sleep(0.25)

    def page_watchdog(win):
        """页面自愈：WSL 重启/桥短暂离线会让 WebView2 停在错误页（JS 全灭），
        桥恢复后自动 load_url 重载。每 10s 体检一次。"""
        time.sleep(4)
        api.apply_transparency_key()
        api.setup_tray()
        api.resize_for('sliver')   # 启动后归一几何（修正 create_window 的 DPI 偏差）
        time.sleep(4)
        url = f"{BRIDGE}/?poll={CFG['poll_ms']}"
        dead_rounds = 0
        reload_fails = 0   # 连续 load_url 未救活次数（心跳恢复才清零）
        while True:
            # 活体判定走桥心跳：页面每 poll_ms 拉一次 /api/state，
            # client_age 持续增大 = 页面真死。不依赖 evaluate_js（会被锁堵死）。
            try:
                with urllib.request.urlopen(f'{BRIDGE}/api/health', timeout=2) as r:
                    health = json.loads(r.read())
                age = health.get('client_age', -1)
                if age < 0 or age > 15:
                    dead_rounds += 1
                    if dead_rounds >= 2:
                        reload_fails += 1
                        if reload_fails >= 3:
                            # WebView2 渲染器死透（load_url 也救不活）→
                            # 最后一级自愈：整进程原参重启（2026-06-11 16:50 实案）
                            _self_restart()
                        _log(f'page heartbeat lost (client_age={age}), reloading '
                             f'({reload_fails}/3)')
                        win.load_url(url)
                        dead_rounds = 0
                        time.sleep(8)
                else:
                    dead_rounds = 0
                    reload_fails = 0
            except OSError:
                dead_rounds = 0          # 桥不在：页面自己会显示离线，别折腾
            time.sleep(10)

    def post_start(win):
        _log('post_start: threads launching')
        threading.Thread(target=cursor_watch, args=(win,), daemon=True).start()
        page_watchdog(win)

    webview.start(post_start, window, debug=args.debug, gui='edgechromium')


if __name__ == '__main__':
    main()
