# A tiny sign-in service for testing shared logins across engines.
import http.server, urllib.parse, html
PAGE = """<!doctype html><title>{title}</title>
<body style="font:22px sans-serif;margin:30px;background:{bg}">
<h1 style="margin:0 0 10px">{title}</h1>
<p id=engine></p>
<p style="font-size:30px"><b>{status}</b></p>
<p><a href="/login?u=alice">Sign in as alice</a> &nbsp; <a href="/logout">Sign out</a>
 &nbsp; <button id=pop style="font:20px sans-serif">Sign in with popup</button>
 &nbsp; <button id=conf style="font:20px sans-serif">Confirm test</button></p>
<p id=msg style="color:#555"></p>
<script>
const ua=navigator.userAgent;
document.getElementById('engine').textContent = /Chrome[/]/.test(ua) ? 'Engine: Blink (Chromium)'
  : /Firefox[/]/.test(ua) ? 'Engine: Gecko (Firefox)' : /AppleWebKit[/]/.test(ua) ? 'Engine: WebKit' : 'Engine: unknown';
document.getElementById('pop').onclick = () => window.open('/popup-login?u=carol', 'signin', 'width=500,height=400');
window.addEventListener('message', e => {{ if (String(e.data).startsWith('signed-in:')) {{
  document.getElementById('msg').textContent = 'Popup reported: ' + e.data + ' (reloading)';
  setTimeout(() => location.reload(), 800); }} }});
document.getElementById('conf').onclick = () => {{
  document.getElementById('msg').textContent = 'confirm() returned: ' + confirm('Proceed with test?'); }};
</script>"""
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def user(self):
        for part in self.headers.get('Cookie', '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == 'session' and v: return v
    def send_page(self, body, cookies=()):
        b = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        for c in cookies: self.send_header('Set-Cookie', c)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def redirect(self, cookie):
        self.send_response(302); self.send_header('Location', '/')
        self.send_header('Set-Cookie', cookie); self.end_headers()
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == '/favicon.ico':
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#d81b60"/></svg>'
            self.send_response(200); self.send_header('Content-Type', 'image/svg+xml')
            self.send_header('Content-Length', str(len(svg))); self.end_headers(); self.wfile.write(svg); return
        if u.path == '/tracker-test':
            return self.send_page("""<!doctype html><title>Tracker test</title>
<body style="font:22px sans-serif;margin:30px"><h1>Tracker test</h1>
<p>This page loads a third-party test tracker (itisatracker.org, from Firefox's built-in test list)
and a third-party image that is not a tracker.</p>
<img src="https://itisatracker.org/pixel.gif?from=tracker-test" width=1 height=1 alt="">
<img src="https://example.org/not-a-tracker.gif" width=1 height=1 alt="">
<p><a href="http://itisatrap.org/firefox/its-a-trap.html">Open Firefox's test phishing page</a></p>""")
        if u.path == '/ads-test':
            return self.send_page("""<!doctype html><title>Ads test</title>
<body style="font:22px sans-serif;margin:30px"><h1>Ads test</h1>
<p id=engine></p><p id=ads style="font-size:26px"><b>checking…</b></p>
<script src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
<img src="https://ad.doubleclick.net/ddm/ad/N1234.example/B5678;sz=300x250" width=1 height=1 alt="">
<script>
const ua=navigator.userAgent;
document.getElementById('engine').textContent = /Chrome[/]/.test(ua) ? 'Engine: Blink (Chromium)' : /Firefox[/]/.test(ua) ? 'Engine: Gecko (Firefox)' : 'Engine: WebKit';
const a = window.adsbygoogle;
document.getElementById('ads').innerHTML = a && a.loaded
  ? '<b style="color:#1d7f4e">Ad script replaced by uBlock Origin’s stand-in</b>'
  : '<b style="color:#b4232a">No stand-in: the ad script was not intercepted</b>';
</script>""")
        if u.path == '/unsupported':
            return self.send_page("""<!doctype html><title>Video Calls</title>
<body style="font:22px sans-serif;margin:30px"><h1>Video Calls</h1>
<p><b>Your browser is not supported.</b> Please use Google Chrome or Microsoft Edge.</p>""")
        if u.path == '/login':
            return self.redirect('session=%s; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax' % q.get('u', ['alice'])[0])
        if u.path == '/logout':
            return self.redirect('session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax')
        if u.path == '/popup-login':
            name = q.get('u', ['carol'])[0]
            return self.send_page("""<!doctype html><title>Sign-in popup</title>
<body style="font:22px sans-serif;margin:30px;background:#fff3cd"><h1>Signing you in as %s…</h1>
<script>setTimeout(() => {{ if (window.opener) {{ window.opener.postMessage('signed-in:%s', '*'); }}
 else {{ document.body.insertAdjacentHTML('beforeend','<p>No window.opener: the page that opened me can\\'t hear back.</p>'); return; }}
 window.close(); }}, 1200);</script>""".replace('{{','{').replace('}}','}') % (html.escape(name), name),
                cookies=['session=%s; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax' % name])
        who = self.user()
        status = ('Signed in as ' + html.escape(who)) if who else 'Not signed in'
        self.send_page(PAGE.format(title='Example Account', status=status, bg='#e6f4ec' if who else '#fdeaea'))
http.server.ThreadingHTTPServer(('127.0.0.1', 8765), H).serve_forever()
