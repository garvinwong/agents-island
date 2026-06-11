import urllib.request, sys, time
t0 = time.time()
try:
    r = urllib.request.urlopen('http://127.0.0.1:5599/api/health', timeout=5)
    print('urllib OK', r.read()[:40], f'{time.time()-t0:.2f}s')
except Exception as e:
    print('urllib FAIL', type(e).__name__, e, f'{time.time()-t0:.2f}s')
import webview
print('pywebview', webview.__version__, 'screens', [(s.width, s.height) for s in webview.screens])
