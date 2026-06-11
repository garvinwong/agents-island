"""js_api 内真实 resize/move：复现岛的窗口缩放路径"""
import time
LOGF = open("D:/OMD-Workspace/apps/agents-island/probe_resize.log", "w", buffering=1)
import builtins
_p = builtins.print
def print(*a, **k): k.setdefault("file", LOGF); _p(*a, **k); LOGF.flush()
import webview

GEOM = {'sliver': (220, 14), 'compact': (380, 80),
        'approval': (484, 162), 'expanded': (530, 520)}
SW = webview.screens[0].width
print(f"start SW={SW}")

class Api:
    window = None
    def resize_for(self, mode, h=0):
        w, hh = GEOM.get(mode, (380, 80))
        if mode == 'expanded' and h:
            hh = int(h) + 40
        print(f'resize_for {mode} ({w}x{hh})', flush=True)
        self.window.resize(w, hh)
        self.window.move((SW - w) // 2, 0)
        print(f'resize_for {mode} done', flush=True)
        return True

api = Api()
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500',
                          js_api=api,
                          width=220, height=14, x=(SW - 220) // 2, y=0,
                          frameless=True, easy_drag=False, on_top=True,
                          transparent=True, focus=False, shadow=False,
                          min_size=(220, 14), background_color='#000000')
api.window = w

def chk(win):
    import urllib.request, json
    time.sleep(4)
    # 注入审批触发 approval → resize
    req = urllib.request.Request('http://127.0.0.1:5599/api/test/enqueue',
        data=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'probe resize'}}).encode(),
        method='POST')
    urllib.request.urlopen(req)
    for i in range(5):
        time.sleep(3)
        try:
            mode = win.evaluate_js('window.__island ? window.__island.mode : "NOJS"')
            print(f't+{(i+1)*3}s mode={mode} win=({win.x},{win.y},{win.width},{win.height})', flush=True)
        except Exception as e:
            print(f't+{(i+1)*3}s ERR {type(e).__name__}', flush=True)
    win.destroy()

webview.start(chk, w, gui='edgechromium')
