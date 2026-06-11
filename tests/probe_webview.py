"""逐项排查 pywebview 参数：哪个 flag 导致页面/JS 不工作。"""
import sys, time, threading
import webview

CASE = sys.argv[1] if len(sys.argv) > 1 else 'plain'
KW = dict(width=400, height=200, x=100, y=100)
if CASE in ('frameless', 'all', 'notrans'):
    KW.update(frameless=True, easy_drag=False)
if CASE in ('ontop', 'all', 'notrans'):
    KW.update(on_top=True)
if CASE in ('trans', 'all'):
    KW.update(transparent=True)
if CASE == 'all':
    KW.update(focus=False, shadow=False, min_size=(220, 14))
if CASE == 'notrans':
    KW.update(focus=False, shadow=False, min_size=(220, 14))

print(f'case={CASE} kw={KW}', flush=True)
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500', **KW)

def chk(win):
    for i in range(8):
        time.sleep(2)
        try:
            url = win.get_current_url()
            mode = win.evaluate_js('window.__island ? window.__island.mode : "NOJS"')
            print(f'  t+{(i+1)*2}s url={url} mode={mode}', flush=True)
            if mode and mode != 'NOJS':
                print('PAGE ALIVE ✔', flush=True)
                break
        except Exception as e:
            print(f'  t+{(i+1)*2}s ERR {type(e).__name__}: {e}', flush=True)
    win.destroy()

webview.start(chk, w, gui='edgechromium')
print('start returned', flush=True)
