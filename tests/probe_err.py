import sys, time
import webview
FRAMELESS = sys.argv[1] == 'frameless' if len(sys.argv) > 1 else True
KW = dict(width=400, height=200, x=100, y=100)
if FRAMELESS:
    KW.update(frameless=True, easy_drag=False)
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500', **KW)
def chk(win):
    time.sleep(5)
    try:
        txt = win.evaluate_js('document.body.innerText.slice(0, 300)')
        print(f'frameless={FRAMELESS} body_text={txt!r}', flush=True)
    except Exception as e:
        print('ERR', e, flush=True)
    win.destroy()
webview.start(chk, w, gui='edgechromium')
