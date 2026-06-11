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
  python <repo>\\win\\island.py
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
    '--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion')

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

CONFIG_FILE = Path(__file__).with_name('island_config.json')
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
GEOM = {
    'sliver':   (220, 14),
    'compact':  (380, 80),
    'approval': (484, 162),
    'expanded': (530, 520),   # 高度由 JS 传入的内容高度覆盖
}


LOG = Path(__file__).with_name('island_win.log')


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
        if mode == 'expanded' and content_h:
            h = int(content_h) + 40        # 内容高 + 投影留白
        try:
            hwnd = self._hwnd()
            user32 = ctypes.windll.user32
            scale = user32.GetDpiForWindow(hwnd) / 96.0
            pw, ph = int(w * scale), int(h * scale)
            sw = user32.GetSystemMetrics(0)            # 物理屏宽（进程已 DPI aware）
            x = (sw - pw) // 2
            y = int(CFG['top_margin'] * scale)
            # 窗口已由 pywebview 置顶；不动 Z 序（跨线程改 TOPMOST 会被拒）
            # SWP_NOZORDER=0x4 | SWP_NOACTIVATE=0x10
            ctypes.set_last_error(0)
            ok = user32.SetWindowPos(hwnd, None, x, y, pw, ph, 0x0014)
            err = ctypes.get_last_error()
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
                c = Color.FromArgb(1, 1, 1)
                form.BackColor = c
                form.TransparencyKey = c
                form.ShowInTaskbar = False
            form.Invoke(System.Action(_apply))
            _log('transparency key + taskbar hidden applied')
        except Exception as e:
            _log(f'transparency key failed: {type(e).__name__}: {e}')

    def setup_tray(self):
        """系统托盘常驻图标：左键/菜单控制面板，退出走托盘。"""
        try:
            import System
            from System.Drawing import Icon
            import System.Windows.Forms as WF
            form = self._window.native
            ico_path = str(Path(__file__).with_name('island.ico'))

            frames_dir = Path(__file__).with_name('tray_frames')
            frame_paths = sorted(frames_dir.glob('f*.ico'))

            def _build():
                form.Icon = Icon(ico_path)          # 窗口/Alt-Tab 图标
                tray = WF.NotifyIcon()
                tray.Icon = Icon(ico_path)
                tray.Text = 'Agents Island'
                # working 时托盘 logo 卫星公转（WinForms Timer 在 UI 线程跳帧）
                frames = [Icon(str(fp)) for fp in frame_paths]
                state = {'i': -1}
                if frames:
                    timer = WF.Timer()
                    timer.Interval = 160

                    def _tick(s, e):
                        if self._working > 0:
                            state['i'] = (state['i'] + 1) % len(frames)
                            tray.Icon = frames[state['i']]
                        elif state['i'] != -1:
                            state['i'] = -1
                            tray.Icon = frames[0]
                    timer.Tick += _tick
                    timer.Start()
                    self._tray_timer = timer        # 保引用防 GC
                menu = WF.ContextMenuStrip()

                def _js(script):
                    def h(s, e):
                        eval_js_nowait(self._window, script)
                    return h

                def _quit(s, e):
                    tray.Visible = False
                    tray.Dispose()
                    self._window.destroy()

                def _reload(s, e):
                    try:
                        self._window.load_url(f"{BRIDGE}/?poll={CFG['poll_ms']}")
                    except Exception:
                        pass

                toggle_js = ("window.__island && __island.setMode("
                             "__island.mode === 'expanded' ? 'sliver' : 'expanded')")
                menu.Items.Add('展开/收起面板 (Ctrl+Alt+E)').Click += _js(toggle_js)
                menu.Items.Add('重载页面').Click += _reload
                menu.Items.Add(WF.ToolStripSeparator())
                menu.Items.Add('退出 (Ctrl+Alt+Q)').Click += _quit
                tray.ContextMenuStrip = menu
                tray.DoubleClick += _js(toggle_js)
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
HOTKEYS = {1: ('A', "islandHotkey('allow')"),
           2: ('D', "islandHotkey('deny')"),
           3: ('S', "islandHotkey('always')"),
           4: ('Q', None),                     # Q = 退出
           5: ('E', "window.__island && __island.setMode("
                    "__island.mode === 'expanded' ? 'sliver' : 'expanded')")}  # E = 开关面板
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
            js = HOTKEYS[msg.wParam][1]
            try:
                if js is None:
                    api.quit()
                    break
                elif api._window:
                    eval_js_nowait(api._window, js)
            except Exception:
                pass
    for hk_id in registered:
        user32.UnregisterHotKey(None, hk_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--debug', action='store_true', help='开 WebView2 DevTools')
    args = ap.parse_args()

    # 单实例互斥：重复双击启动器直接静默退出
    ctypes.windll.kernel32.CreateMutexW(None, False, 'AgentsIslandSingleton')
    if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        sys.exit(0)

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
        transparent=True,
        easy_drag=False,
        focus=False,
        shadow=False,
        min_size=(GEOM['sliver'][0], GEOM['sliver'][1]),   # 放开默认 200×100 下限
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
        time.sleep(6)
        hwnd = api._hwnd()
        while True:
            try:
                user32.GetCursorPos(ctypes.byref(pt))
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                inside = rect.left <= pt.x <= rect.right and rect.top <= pt.y <= rect.bottom
                if inside != last:
                    r = eval_js_timeout(
                        win, f'window.islandCursor && islandCursor({str(inside).lower()})', 1.5)
                    if r != '__timeout__':
                        last = inside
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
        was_dead = False
        while True:
            probe = eval_js_timeout(win, 'window.__island ? "ok" : "dead"', 3.0)
            alive = 'ok' if probe == 'ok' else 'dead'
            if alive != 'ok':
                if not was_dead:
                    _log('page dead, waiting for bridge...')
                was_dead = True
                try:
                    with urllib.request.urlopen(f'{BRIDGE}/api/health', timeout=2):
                        _log('bridge back, reloading page')
                        win.load_url(url)
                        time.sleep(5)
                except OSError:
                    pass
            elif was_dead:
                _log('page recovered')
                was_dead = False
            time.sleep(10)

    def post_start(win):
        threading.Thread(target=cursor_watch, args=(win,), daemon=True).start()
        page_watchdog(win)

    webview.start(post_start, window, debug=args.debug, gui='edgechromium')


if __name__ == '__main__':
    main()
