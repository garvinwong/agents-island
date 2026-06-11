"""js_api 组合排查：plain+api / all+api / all+api+hotkey"""
import sys, time, threading
import webview

CASE = sys.argv[1]

class Api:
    def resize_for(self, mode, h=0):
        return True

KW = dict(width=400, height=200, x=100, y=100)
if CASE in ('allapi', 'allapihk'):
    KW.update(frameless=True, easy_drag=False, on_top=True, transparent=True,
              focus=False, shadow=False, min_size=(220, 14))

api = Api()
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500',
                          js_api=api, **KW)

def chk(win):
    for i in range(5):
        time.sleep(3)
        try:
            mode = win.evaluate_js('window.__island ? window.__island.mode : "NOJS"')
            pwv = win.evaluate_js('typeof window.pywebview')
            print(f't+{(i+1)*3}s mode={mode} pywebview={pwv}', flush=True)
        except Exception as e:
            print(f't+{(i+1)*3}s ERR {e}', flush=True)
    win.destroy()

if CASE == 'allapihk':
    import ctypes, ctypes.wintypes
    def hk():
        u = ctypes.windll.user32
        u.RegisterHotKey(None, 1, 0x0003, ord('A'))
        msg = ctypes.wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
    threading.Thread(target=hk, daemon=True).start()

webview.start(chk, w, gui='edgechromium')
