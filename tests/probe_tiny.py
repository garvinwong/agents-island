"""微型初始窗口 + 全 flag + js_api：复现岛的精确启动参数"""
import sys, time
import webview

class Api:
    def resize_for(self, mode, h=0):
        return True

CASE = sys.argv[1] if len(sys.argv) > 1 else 'tiny'
H = {'tiny': 14, 'h40': 40, 'h80': 80}[CASE]
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500',
                          js_api=Api(),
                          width=220, height=H, x=610, y=0,
                          frameless=True, easy_drag=False, on_top=True,
                          transparent=True, focus=False, shadow=False,
                          min_size=(220, H), background_color='#000000')

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
