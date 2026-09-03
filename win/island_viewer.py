#!/usr/bin/env python3
"""
Agents Island — 查看窗独立进程（每窗一进程）
==========================================
由主岛 island.py 的 show worker 以 `pythonw island_viewer.py <kind> <win_path> [name] [--raw]`
拉起。走 pywebview 的「主窗路径」（create_window + start），不再在主岛进程里
运行态动态开子窗——那条路径在 WebView2 Runtime 152 (2026-09-03 Evergreen 升级)
下会把主岛 UI 线程拖死，且任何重型页面都能连坐全岛。进程隔离后：毒页面、
Runtime 回归、渲染器崩溃，最多死本窗自己。

健壮性：
  - ready 握手：页面 pywebviewready 后 js_api.ready() 往 stdout 写一行 READY，主岛据此判活
  - 看门狗：READY_TIMEOUT 内页面没就绪 → 自毁 + 兜底 os.startfile 交系统默认程序
  - 任何异常退出码非 0，主岛侧按退出码回落 startfile
"""
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

RES_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
READY_TIMEOUT = 12.0     # 页面就绪看门狗（重 md/大图 UNC 加载留余量）
KINDS = ('image', 'html', 'pdf', 'md')

# 主岛同款 WebView2 启动参数（防后台节流冻结）；须在 import webview 之前
os.environ.setdefault(
    'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS',
    '--disable-background-timer-throttling '
    '--disable-backgrounding-occluded-windows '
    '--disable-renderer-backgrounding '
    '--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion')
# 与主岛同一 AUMID：任务栏归同一组、图标走窗口 Icon
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('AgentsIsland.App')
except OSError:
    pass

import webview  # noqa: E402


def _log(msg: str):
    """stderr 行日志（主岛 worker 收集转写进 island_win.log）。"""
    try:
        sys.stderr.write(f'{time.strftime("%H:%M:%S")} [viewer] {msg}\n')
        sys.stderr.flush()
    except OSError:
        pass


def _emit(line: str):
    """stdout 协议行（READY / FALLBACK）。"""
    try:
        sys.stdout.write(line + '\n')
        sys.stdout.flush()
    except OSError:
        pass


def _file_uri(p: str) -> str:
    """Windows 路径 → file URI。盘符 D:\\x → file:///D:/x；
    UNC \\\\wsl.localhost\\.. → file://wsl.localhost/..（host 位即 UNC 主机）"""
    from urllib.parse import quote
    q = quote(p.replace('\\', '/'), safe='/:')
    return ('file:' + q) if q.startswith('//') else ('file:///' + q)


# 查看窗曜石壳（血统同岛：近黑底+顶缘受光+琥珀呼吸点）。模板用 __TOKEN__
# 置换而非 f-string——CSS/JS 花括号密集，f-string 转义地狱。
# 窗口是 frameless（Owner 嫌 WinForms 原生标题栏是"壳外壳"），所以拖动/
# 三键/缩放握把全部自绘：#bar 挂 pywebview-drag-region（customize.js 原生
# 机制），三键与握把放 drag 区外走 _ViewerCtl js_api
_VIEW_CHROME_CSS = """
html,body{margin:0;height:100%;background:#0b0c0e;overflow:hidden;
  font:12px 'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif;color:#cfd3d8}
#bar{height:34px;display:flex;align-items:center;gap:10px;padding:0 132px 0 12px;
  background:linear-gradient(180deg,rgba(255,255,255,.075),rgba(255,255,255,.028) 55%,rgba(255,255,255,.012));
  border-bottom:1px solid rgba(255,255,255,.09);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.16);user-select:none;
  white-space:nowrap;overflow:hidden}
#dot{flex:none;width:7px;height:7px;border-radius:50%;background:#D97757;
  box-shadow:0 0 8px rgba(217,119,87,.8);animation:br 4.2s ease-in-out infinite}
@keyframes br{0%,100%{opacity:1}50%{opacity:.45}}
#name{overflow:hidden;text-overflow:ellipsis;letter-spacing:.2px}
#zoom{color:#9ba1a8;font-variant-numeric:tabular-nums}
#hint{margin-left:auto;color:#63676d;flex:none}
#ctl{position:fixed;top:0;right:0;height:34px;display:flex;z-index:9}
#ctl button{width:40px;height:34px;border:0;background:transparent;color:#9ba1a8;
  font:13px 'Segoe UI';cursor:pointer;padding:0}
#ctl button:hover{background:rgba(255,255,255,.08);color:#e8eaec}
#c-close:hover{background:rgba(196,43,28,.85)!important;color:#fff!important}
#grip{position:fixed;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;
  z-index:9;clip-path:polygon(100% 0,100% 100%,0 100%);
  background:repeating-linear-gradient(135deg,transparent 0 4px,rgba(255,255,255,.25) 4px 5px)}
#edge{position:fixed;inset:0;pointer-events:none;z-index:99;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.09)}
"""

# 窗控三件套+缩放握把+Esc（两模板共用；frameless 后这些就是全部的窗口管理）
_VIEW_CTL_HTML = """
<div id=ctl>
  <button id=c-min title="最小化">─</button>
  <button id=c-max title="最大化/还原">□</button>
  <button id=c-close title="关闭 (Esc)">✕</button>
</div>
<div id=grip title="拖动调整大小"></div>
<div id=edge></div>
<script>
(() => {
  const api = () => window.pywebview && window.pywebview.api;
  const q = id => document.getElementById(id);
  window.addEventListener('pywebviewready', () => api() && api().ready());
  q('c-min').onclick = () => api() && api().win_min();
  q('c-max').onclick = () => api() && api().win_max();
  q('c-close').onclick = () => api() && api().win_close();
  // 双击顶栏最大化/还原（Windows 标题栏直觉）。顶栏虽是拖动区，但 customize.js
  // 的拖动只在 mousemove 才动窗，干净双击不受影响
  const bar = document.querySelector('.pywebview-drag-region');
  if (bar) bar.addEventListener('dblclick', () => api() && api().win_max());
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape' && api()) api().win_close(); });
  const g = q('grip');
  let on = false, t = 0;
  g.addEventListener('pointerdown', e => { on = true;
    g.setPointerCapture(e.pointerId); e.preventDefault(); e.stopPropagation(); });
  g.addEventListener('pointermove', e => { if (!on) return;
    const now = performance.now(); if (now - t < 30) return; t = now;   // 节流防淹 js_api 桥
    if (api()) api().win_resize(Math.max(360, e.clientX + 9),
                                Math.max(240, e.clientY + 9)); });
  g.addEventListener('pointerup', () => { on = false; });
})();
</script>"""

_VIEW_IMAGE_TPL = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>__CSS__
#stage{position:fixed;inset:34px 0 0 0;overflow:hidden;cursor:grab;
  background:radial-gradient(120% 90% at 50% 0%,#101216 0%,#0b0c0e 55%,#080909 100%)}
#stage.drag{cursor:grabbing}
#im{position:absolute;left:0;top:0;transform-origin:0 0;
  box-shadow:0 8px 40px rgba(0,0,0,.55)}
</style>
<div id=bar class=pywebview-drag-region><span id=dot></span><span id=name>__TITLE__</span>
  <span id=zoom></span><span id=hint>滚轮缩放 · 拖动平移 · 双击适屏/原始</span></div>
<div id=stage><img id=im src="__URI__"></div>__CTL__
<script>
(() => {
  const st = document.getElementById('stage'), im = document.getElementById('im'),
        zl = document.getElementById('zoom');
  let s = 1, x = 0, y = 0, fit = 1, nw = 0, nh = 0;
  const apply = () => { im.style.transform = `translate(${x}px,${y}px) scale(${s})`;
                        zl.textContent = Math.round(s * 100) + '%'; };
  const fitView = () => { const r = st.getBoundingClientRect();
    fit = Math.min(r.width / nw, r.height / nh, 1);
    s = fit; x = (r.width - nw * s) / 2; y = (r.height - nh * s) / 2; apply(); };
  im.onload = () => { nw = im.naturalWidth; nh = im.naturalHeight; fitView(); };
  // preventDefault 同时压掉 WebView2 自身的 Ctrl+滚轮页面缩放，缩放全归这里
  st.addEventListener('wheel', e => { e.preventDefault();
    const r = st.getBoundingClientRect(),
          px = e.clientX - r.left, py = e.clientY - r.top,
          ns = Math.min(Math.max(s * (e.deltaY < 0 ? 1.15 : 1 / 1.15), .05), 40);
    x = px - (px - x) * ns / s; y = py - (py - y) * ns / s; s = ns; apply();
  }, {passive: false});
  let dg = null;
  st.addEventListener('pointerdown', e => { dg = [e.clientX - x, e.clientY - y];
    st.classList.add('drag'); st.setPointerCapture(e.pointerId); });
  st.addEventListener('pointermove', e => { if (!dg) return;
    x = e.clientX - dg[0]; y = e.clientY - dg[1]; apply(); });
  st.addEventListener('pointerup', () => { dg = null; st.classList.remove('drag'); });
  st.addEventListener('dblclick', e => {
    if (Math.abs(s - fit) > .001) { fitView(); return; }
    const r = st.getBoundingClientRect(),
          px = e.clientX - r.left, py = e.clientY - r.top;
    x = px - (px - x) / s; y = py - (py - y) / s; s = 1; apply();   // 原始尺寸，锚点击处
  });
  window.addEventListener('resize', () => { if (Math.abs(s - fit) < .001) fitView(); });
})();
</script>"""

_VIEW_HTML_TPL = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>__CSS__
iframe{position:fixed;inset:34px 0 0 0;width:100%;height:calc(100% - 34px);
  border:0;background:#fff}
</style>
<div id=bar class=pywebview-drag-region><span id=dot></span><span id=name>__TITLE__</span>
  <span id=hint>__HINT__</span></div>
<iframe src="__URI__" sandbox="allow-scripts"></iframe>__CTL__"""

# MD 阅读器：Windows 侧读文件、正文以 JSON 内嵌（__MDJSON__），客户端渲染。
# 渲染器是岛 plan 审阅 mdToHtml 的增强版（表格/链接/引用/hr），仍零外部依赖
_VIEW_MD_TPL = """<!doctype html><meta charset="utf-8"><title>__TITLE__</title>
<style>__CSS__
#doc{position:fixed;inset:34px 0 0 0;overflow:auto}
article{max-width:880px;margin:30px auto 72px;padding:0 40px;
  font:15px/1.8 'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif;color:#c9cdd3}
article h1{font-size:26px;color:#eceef0;margin:26px 0 14px;letter-spacing:.3px}
article h2{font-size:20px;color:#e4e7ea;margin:30px 0 12px;padding-bottom:7px;
  border-bottom:1px solid rgba(255,255,255,.10)}
article h3{font-size:16.5px;color:#dde0e4;margin:22px 0 8px}
article h4{font-size:15px;color:#d4d8dc;margin:18px 0 6px}
article p{margin:9px 0}
article a{color:#D97757;text-decoration:none}
article a:hover{text-decoration:underline}
article code{background:rgba(217,119,87,.13);color:#e8b49e;padding:1px 6px;
  border-radius:4px;font:13px Consolas,monospace}
article pre{background:#14161a;border:1px solid rgba(255,255,255,.08);
  border-radius:8px;padding:14px 16px;overflow-x:auto;margin:14px 0}
article pre code{background:none;color:#c9cdd3;padding:0}
article ul{margin:8px 0;padding-left:26px}
article li{margin:4px 0}
article blockquote{background:rgba(255,255,255,.045);border-radius:8px;
  padding:10px 18px;margin:12px 0;color:#a9aeb5}
article hr{border:0;border-top:1px solid rgba(255,255,255,.10);margin:24px 0}
article table{border-collapse:collapse;margin:14px 0;width:100%}
article th,article td{border:1px solid rgba(255,255,255,.10);padding:7px 12px;
  text-align:left;font-size:13.5px}
article th{background:rgba(255,255,255,.055);color:#e0e3e6}
article img{max-width:100%}
</style>
<div id=bar class=pywebview-drag-region><span id=dot></span><span id=name>__TITLE__</span>
  <span id=hint>Markdown · Ctrl+滚轮 缩放</span></div>
<div id=doc><article id=out></article></div>
<script>const MD_SRC = __MDJSON__;</script>
<script>
(() => {
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const codes = [];
  let src = MD_SRC.replace(/```\\w*\\n?([\\s\\S]*?)```/g,
    (m, c) => { codes.push(c); return '\\x00' + (codes.length - 1) + '\\x00'; });
  let h = esc(src);
  h = h.replace(/^\\|(.+)\\|[ \\t]*\\n\\|[ \\t:|-]+\\|[ \\t]*\\n((?:\\|.*\\|[ \\t]*\\n?)*)/gm,
    (m, head, body) => {
      const cells = r => r.split('|').slice(1, -1).map(c => c.trim());
      const th = cells('|' + head + '|').map(c => `<th>${c}</th>`).join('');
      const rows = body.trim().split('\\n').filter(Boolean).map(r =>
        '<tr>' + cells(r).map(c => `<td>${c}</td>`).join('') + '</tr>').join('');
      return `<table><thead><tr>${th}</tr></thead><tbody>${rows}</tbody></table>\\n\\n`;
    });
  h = h.replace(/^#### (.*)$/gm, '<h4>$1</h4>')
       .replace(/^### (.*)$/gm, '<h3>$1</h3>')
       .replace(/^## (.*)$/gm, '<h2>$1</h2>')
       .replace(/^# (.*)$/gm, '<h1>$1</h1>')
       .replace(/^(---+|\\*\\*\\*+)$/gm, '<hr>')
       .replace(/^&gt; ?(.*)$/gm, '<blockquote>$1</blockquote>')
       .replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>')
       .replace(/(^|[^*])\\*([^*\\n]+)\\*/g, '$1<i>$2</i>')
       .replace(/`([^`]+)`/g, '<code>$1</code>')
       .replace(/\\[([^\\]]+)\\]\\(([^)\\s]+)\\)/g, '<a href="$2">$1</a>')
       .replace(/^[-*] (.*)$/gm, '<li>$1</li>')
       .replace(/^\\d+\\. (.*)$/gm, '<li>$1</li>');
  h = h.split(/\\n{2,}/).map(b => {
    b = b.trim();
    if (/^<(h\\d|li|pre|table|blockquote|hr)/.test(b)) {
      if (b.startsWith('<li')) b = '<ul>' + b.replace(/\\n(?=<li)/g, '') + '</ul>';
      return b.replace(/\\n(?=<(li|blockquote|tr))/g, '');
    }
    return b ? `<p>${b.replace(/\\n/g, '<br>')}</p>` : '';
  }).join('');
  h = h.replace(/\\x00(\\d+)\\x00/g,
    (m, i) => `<pre><code>${esc(codes[+i])}</code></pre>`);
  document.getElementById('out').innerHTML = h;
})();
</script>__CTL__"""


class _ViewerCtl:
    """查看窗的窗口控制 js_api——frameless 后最小化/最大化/关闭/缩放全靠它。
    每窗独立实例；_w 在 create_window 返回后回填（用户点击远晚于回填，无竞态）。"""

    def __init__(self):
        self._w = None
        self.ready_ev = threading.Event()

    def ready(self) -> bool:
        """页面 pywebviewready 后调用：向主岛报就绪，解除看门狗。"""
        if not self.ready_ev.is_set():
            self.ready_ev.set()
            _emit('READY')
            _log('page ready')
        return True

    def win_close(self) -> bool:
        try:
            self._w.destroy()
        except Exception:
            pass
        return True

    def win_min(self) -> bool:
        try:
            self._w.minimize()
        except Exception:
            pass
        return True

    def win_max(self) -> bool:
        try:
            if str(self._w.native.WindowState) == 'Maximized':
                self._w.restore()
            else:
                self._w.maximize()
        except Exception as e:
            _log(f'viewer max err: {e}')
        return True

    def win_resize(self, w, h) -> bool:
        """握把缩放。pywebview 的 resize 不乘 DPI（岛内前科），Win32 物理像素直设；
        frameless 无边框装饰，CSS client 尺寸=窗口外尺寸，换算干净。"""
        try:
            hwnd = int(str(self._w.native.Handle))
            u = ctypes.windll.user32
            scale = u.GetDpiForWindow(hwnd) / 96.0
            u.SetWindowPos(hwnd, None, 0, 0, int(w * scale), int(h * scale),
                           0x0002 | 0x0004 | 0x0010)   # NOMOVE|NOZORDER|NOACTIVATE
        except Exception as e:
            _log(f'viewer resize err: {e}')
        return True

def _esc_html(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _read_md(win_path: str, timeout: float = 8.0):
    """限时读 md：UNC(9P) 在 WSL 高负载时 open() 会无限挂起不抛错。"""
    box = {}

    def _rd():
        try:
            with open(win_path, encoding='utf-8', errors='replace') as f:
                box['t'] = f.read(2 * 1024 * 1024)   # 2MB 封顶
        except OSError as e:
            box['e'] = str(e)
    t = threading.Thread(target=_rd, daemon=True)
    t.start()
    t.join(timeout)
    if 't' not in box:
        raise OSError(box.get('e', f'read timeout {timeout}s (9P hang?)'))
    return box['t']


def build_url(kind: str, win_path: str, title: str, raw: bool) -> str:
    """产出窗口要加载的 URL：raw 直开原文件，否则写曜石壳 wrapper 到 %TEMP%。"""
    if raw and kind in ('html', 'pdf'):
        return _file_uri(win_path)
    tpl = {'image': _VIEW_IMAGE_TPL, 'md': _VIEW_MD_TPL,
           'html': _VIEW_HTML_TPL, 'pdf': _VIEW_HTML_TPL}[kind]
    html = (tpl.replace('__CSS__', _VIEW_CHROME_CSS)
               .replace('__CTL__', _VIEW_CTL_HTML)
               .replace('__TITLE__', _esc_html(title))
               .replace('__HINT__', 'Edge 内置 PDF 阅读器' if kind == 'pdf'
                        else 'Ctrl+滚轮 缩放页面')
               .replace('__URI__', _file_uri(win_path)))
    if kind == 'md':
        md_text = _read_md(win_path)
        # json.dumps 做 JS 字符串转义；'</' 再断开防正文里的 </script> 提前闭合
        html = html.replace('__MDJSON__', json.dumps(md_text).replace('</', '<\\/'))
    wrapper = os.path.join(tempfile.gettempdir(),
                           f'island_view_{os.getpid()}_{int(time.time() * 1000)}.html')
    with open(wrapper, 'w', encoding='utf-8') as f:
        f.write(html)
    return _file_uri(wrapper)


def _fallback(win_path: str, why: str):
    """兜底：交系统默认程序打开，并告知主岛。"""
    _log(f'fallback ({why}): {win_path}')
    _emit('FALLBACK ' + why)
    try:
        os.startfile(win_path)
    except OSError as e:
        _log(f'startfile err: {e}')


def main(argv) -> int:
    if len(argv) < 3 or argv[1] not in KINDS:
        _log(f'bad args: {argv[1:]}')
        return 2
    kind, win_path = argv[1], argv[2]
    raw = '--raw' in argv[3:]
    rest = [a for a in argv[3:] if not a.startswith('--')]
    title = (rest[0] if rest else win_path.rsplit('\\', 1)[-1])[:60]
    wrapped = not (raw and kind in ('html', 'pdf'))

    try:
        url = build_url(kind, win_path, title, raw)
    except Exception as e:
        _fallback(win_path, f'build:{type(e).__name__}')
        return 3

    ctl = _ViewerCtl()
    w = webview.create_window(title, url=url, width=980, height=720,
                              on_top=False, frameless=wrapped,
                              easy_drag=False, zoomable=True, js_api=ctl)
    ctl._w = w
    if not wrapped:
        ctl.ready_ev.set()   # raw 直开无壳脚本，视为立即就绪
        _emit('READY')

    def _chrome():
        try:
            hwnd = int(str(w.native.Handle))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)   # Win11 DWM 圆角
            from webview.platforms import winforms as _wf
            from System.Drawing import Icon   # pythonnet 在 webview.start 后才可用
            f = w.native
            f.MaximizedBounds = _wf.WinForms.Screen.FromControl(f).WorkingArea
            # RES_DIR 在本机部署形态下也是 UNC(9P)：先用限时线程把字节读进内存，
            # 再从内存流构造 Icon——UI 线程零磁盘 I/O，读不到就不设图标
            box = {}
            def _rd():
                try:
                    box['b'] = (RES_DIR / 'island.ico').read_bytes()
                except OSError as e:
                    box['e'] = str(e)
            rt = threading.Thread(target=_rd, daemon=True)
            rt.start(); rt.join(3)
            if 'b' in box:
                from System.IO import MemoryStream
                f.Icon = Icon(MemoryStream(box['b']))
            else:
                _log(f'icon skip: {box.get("e", "read timeout")}')
        except Exception as e:
            _log(f'chrome err: {e}')
    if wrapped:
        w.events.shown += _chrome

    def _watchdog():
        if ctl.ready_ev.wait(READY_TIMEOUT):
            return
        _log(f'page not ready in {READY_TIMEOUT}s — self-destruct')
        _fallback(win_path, 'ready-timeout')
        try:
            w.destroy()
        except Exception:
            pass
    threading.Thread(target=_watchdog, daemon=True).start()

    _log(f'start {kind}: {win_path}')
    webview.start(gui='edgechromium')
    _log('exit')
    return 0


if __name__ == '__main__':
    try:
        code = main(sys.argv)
    except Exception as e:   # 任何未捕获异常：兜底开文件，非零退出让主岛知道
        _log(f'fatal: {type(e).__name__}: {e}')
        if len(sys.argv) > 2:
            _fallback(sys.argv[2], 'fatal')
        code = 1
    os._exit(code)
