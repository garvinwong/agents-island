"""启动前访问 webview.screens + 其余与岛一致"""
import time, urllib.request
import webview

class Api:
    def resize_for(self, mode, h=0):
        return True

# 岛的 wait_bridge 等价调用
urllib.request.urlopen('http://127.0.0.1:5599/api/health', timeout=2)

SW = webview.screens[0].width          # ← 嫌疑调用
print(f'screens width pre-start = {SW}', flush=True)
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500',
                          js_api=Api(),
                          width=220, height=14, x=(SW - 220) // 2, y=0,
                          frameless=True, easy_drag=False, on_top=True,
                          transparent=True, focus=False, shadow=False,
                          min_size=(220, 14), background_color='#000000')

def chk(win):
    for i in range(4):
        time.sleep(3)
        try:
            mode = win.evaluate_js('window.__island ? window.__island.mode : "NOJS"')
            pwv = win.evaluate_js('typeof window.pywebview')
            print(f't+{(i+1)*3}s mode={mode} pywebview={pwv}', flush=True)
        except Exception as e:
            print(f't+{(i+1)*3}s ERR {type(e).__name__}', flush=True)
    win.destroy()

webview.start(chk, w, gui='edgechromium')
