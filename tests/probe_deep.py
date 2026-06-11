"""frameless 下页面内部诊断。"""
import sys, time
import webview

KW = dict(width=400, height=200, x=100, y=100, frameless=True, easy_drag=False)
w = webview.create_window('probe', url='http://127.0.0.1:5599/?poll=500', **KW)

def chk(win):
    time.sleep(5)
    probes = {
        'readyState': 'document.readyState',
        'scripts': 'document.scripts.length',
        'stage_el': 'String(!!document.getElementById("stage"))',
        'island_el': 'String(!!document.getElementById("island"))',
        'typeof_island': 'typeof window.__island',
        'body_children': 'document.body ? document.body.children.length : -1',
        'err_hook': '(window.__lasterr || "none")',
        'csp': '(document.querySelector("meta[http-equiv]") || {}).content || "no-meta"',
        'title': 'document.title',
    }
    for k, js in probes.items():
        try:
            print(f'{k} = {win.evaluate_js(js)}', flush=True)
        except Exception as e:
            print(f'{k} ERR {e}', flush=True)
    win.destroy()

webview.start(chk, w, gui='edgechromium')
