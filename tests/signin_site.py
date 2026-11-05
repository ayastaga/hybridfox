#!/usr/bin/env python3
"""A tiny sign-in site for testing shared logins across engines.

  python3 tests/signin_site.py [port]     (default 8765, on 127.0.0.1)

Besides the pages a person clicks through, it records every request it
serves, so tests can check what each engine really did without reading
pixels:

  /_log     JSON list of {path, query, engine, user, t}: the engine comes
            from the User-Agent, the user from the session cookie
  /_reset   clears the log

Fixture pages used by the tests:

  /                 the account page (signed in or not)
  /login?u=NAME     signs in (session cookie), redirects to /
  /logout           signs out, redirects to /
  /samesite?v=MODE  re-sets the session cookie with SameSite=MODE only
  /popup            a full-page "Sign in with popup" button
  /popup-login      the popup: signs in, tells window.opener, closes itself
  /confirm          calls confirm() on load and reports the answer to /_log
  /red              a red 100x100 square on white (for zoom tests)
  /third-party?via=HOST   loads an image from HOST (another origin)
  /ads-test         an ad script and a DoubleClick image (uBlock Origin)
  /unsupported      a page that says the browser isn't supported
  /tracker-test     Firefox's test tracker and a harmless third-party image

Served JavaScript avoids backslashes (e.g. /Chrome[/]/ rather than
/Chrome\\//): escaping inside Python strings went wrong before.
"""
import html
import http.server
import json
import sys
import threading
import time
import urllib.parse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

LOG = []
LOG_LOCK = threading.Lock()

ENGINE_JS = """const ua = navigator.userAgent;
const engine = /Firefox[/]/.test(ua) ? 'gecko' : /Chrome[/]/.test(ua) ? 'blink'
  : /AppleWebKit[/]/.test(ua) ? 'webkit' : 'unknown';"""

PAGE = """<!doctype html><title>{title}</title>
<body style="font:22px sans-serif;margin:30px;background:{bg}">
<h1 style="margin:0 0 10px">{title}</h1>
<p id=engine></p>
<p style="font-size:30px"><b id=status>{status}</b></p>
<p><a href="/login?u=alice">Sign in as alice</a> &nbsp; <a href="/logout">Sign out</a>
 &nbsp; <button id=pop style="font:20px sans-serif">Sign in with popup</button>
 &nbsp; <button id=conf style="font:20px sans-serif">Confirm test</button></p>
<p id=msg style="color:#555"></p>
<script>
""" + ENGINE_JS + """
document.getElementById('engine').textContent = 'Engine: ' + engine;
document.getElementById('pop').onclick = () => window.open('/popup-login?u=carol', 'signin', 'width=500,height=400');
window.addEventListener('message', e => {{ if (String(e.data).startsWith('signed-in:')) {{
  document.getElementById('msg').textContent = 'Popup reported: ' + e.data + ' (reloading)';
  setTimeout(() => location.reload(), 800); }} }});
document.getElementById('conf').onclick = () => {{
  document.getElementById('msg').textContent = 'confirm() returned: ' + confirm('Proceed with test?'); }};
</script>"""


def engine_of(ua):
    # Order matters: Chromium's UA also says "AppleWebKit" and "Safari".
    if "Firefox/" in ua:
        return "gecko"
    if "Chrome/" in ua:
        return "blink"
    if "AppleWebKit/" in ua:
        return "webkit"
    return "unknown"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def user(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "session" and v:
                return v
        return None

    def send(self, body, ctype="text/html; charset=utf-8", cookies=(), status=200, headers=()):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for c in cookies:
            self.send_header("Set-Cookie", c)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, cookie, to="/"):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        arg = lambda k, d="": q.get(k, [d])[0]
        if u.path not in ("/_log", "/_reset"):
            with LOG_LOCK:
                LOG.append({
                    "path": u.path, "query": u.query, "host": self.headers.get("Host", ""),
                    "engine": engine_of(self.headers.get("User-Agent", "")),
                    "user": self.user(), "t": time.time(),
                })

        if u.path == "/_log":
            with LOG_LOCK:
                return self.send(json.dumps(LOG), "application/json")
        if u.path == "/_reset":
            with LOG_LOCK:
                LOG.clear()
            return self.send("ok", "text/plain")
        if u.path == "/_report":  # pages report results here; the log keeps them
            return self.send("ok", "text/plain")
        if u.path in ("/favicon.ico", "/pixel.svg"):
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="7" fill="#d81b60"/></svg>'
            return self.send(svg, "image/svg+xml")
        if u.path == "/login":
            return self.redirect("session=%s; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax" % arg("u", "alice"))
        if u.path == "/logout":
            return self.redirect("session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        if u.path == "/samesite":
            # Same name, value and attributes as /login, only SameSite differs.
            who = self.user() or "alice"
            return self.redirect("session=%s; Path=/; Max-Age=3600; HttpOnly; SameSite=%s" % (who, arg("v", "Strict")),
                                 to="/?samesite=" + arg("v", "Strict"))
        if u.path == "/popup":
            return self.send("""<!doctype html><title>Popup test</title>
<body style="margin:0"><button id=pop style="position:fixed;inset:0;width:100%;height:100%;font:40px sans-serif;background:#ffd166">
Sign in with popup</button>
<script>
document.getElementById('pop').onclick = () => window.open('/popup-login?u=carol', 'signin', 'width=500,height=400');
window.addEventListener('message', e => {
  document.title = 'got ' + e.data;
  fetch('/_report?popup=' + encodeURIComponent(e.data));
});
</script>""")
        if u.path == "/popup-login":
            name = arg("u", "carol")
            return self.send("""<!doctype html><title>Sign-in popup</title>
<body style="font:22px sans-serif;margin:30px;background:#fff3cd"><h1>Signing you in as %s</h1>
<script>
const hasOpener = !!window.opener;
fetch('/_report?opener=' + hasOpener).then(() => {
  if (hasOpener) window.opener.postMessage('signed-in:%s', '*');
  setTimeout(() => window.close(), 300);
});
</script>""" % (html.escape(name), name), cookies=["session=%s; Path=/; Max-Age=3600; HttpOnly; SameSite=Lax" % name])
        if u.path == "/confirm":
            return self.send("""<!doctype html><title>Confirm test</title>
<body style="font:22px sans-serif;margin:30px"><p id=r>asking</p>
<script>
setTimeout(() => {
  const r = confirm('Proceed with test?');
  document.getElementById('r').textContent = 'confirm returned ' + r;
  document.title = 'confirm ' + r;
  fetch('/_report?confirm=' + r);
}, 200);
</script>""")
        if u.path == "/red":
            return self.send("""<!doctype html><title>Red square</title>
<body style="margin:0;background:#fff"><div style="width:100px;height:100px;background:#f00"></div>""")
        if u.path == "/third-party":
            via = arg("via", "localhost:%d" % PORT)
            return self.send("""<!doctype html><title>Third-party image</title>
<body style="font:22px sans-serif;margin:30px"><h1>Third-party image</h1>
<img src="http://%s/pixel.svg?from=third-party" width=16 height=16 alt="">""" % html.escape(via))
        if u.path == "/stand-in-test":
            # A third-party script; the page reports whether the copy that ran
            # was the real one or a stand-in an extension (or the engine)
            # substituted.
            via = arg("via", "localhost:%d" % PORT)
            return self.send("""<!doctype html><title>Stand-in test</title>
<body style="font:22px sans-serif;margin:30px"><p id=r>checking</p>
<script src="http://%s/thirdparty.js"></script>
<script>
const v = window.thirdPartyVersion || 'none';
document.getElementById('r').textContent = 'script: ' + v;
document.title = 'script ' + v;
fetch('/_report?script=' + v);
</script>""" % html.escape(via))
        if u.path == "/thirdparty.js":
            return self.send("window.thirdPartyVersion = 'real';", "application/javascript")
        if u.path == "/redirected.js":
            return self.send("window.thirdPartyVersion = 'redirected';", "application/javascript")
        if u.path == "/tracker-test":
            return self.send("""<!doctype html><title>Tracker test</title>
<body style="font:22px sans-serif;margin:30px"><h1>Tracker test</h1>
<p>This page loads a third-party test tracker (itisatracker.org, from Firefox's built-in test list)
and a third-party image that is not a tracker.</p>
<img src="https://itisatracker.org/pixel.gif?from=tracker-test" width=1 height=1 alt="">
<img src="https://example.org/not-a-tracker.gif" width=1 height=1 alt="">
<p><a href="http://itisatrap.org/firefox/its-a-trap.html">Open Firefox's test phishing page</a></p>""")
        if u.path == "/ads-test":
            return self.send("""<!doctype html><title>Ads test</title>
<body style="font:22px sans-serif;margin:30px"><h1>Ads test</h1>
<p id=engine></p><p id=ads style="font-size:26px"><b>checking</b></p>
<script src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
<img id=dc src="https://ad.doubleclick.net/ddm/ad/N1234.example/B5678;sz=300x250" width=1 height=1 alt="">
<script>
""" + ENGINE_JS + """
document.getElementById('engine').textContent = 'Engine: ' + engine;
const a = window.adsbygoogle;
const standIn = !!(a && a.loaded);
document.getElementById('ads').innerHTML = standIn
  ? '<b style="color:#1d7f4e">Ad script replaced by a stand-in</b>'
  : '<b style="color:#b4232a">No stand-in: the ad script was not intercepted</b>';
const img = document.getElementById('dc');
const report = loaded => fetch('/_report?standin=' + standIn + '&doubleclick=' + (loaded ? 'loaded' : 'blocked'));
if (img.complete) report(img.naturalWidth > 0);
else { img.onload = () => report(true); img.onerror = () => report(false); }
</script>""")
        if u.path == "/unsupported":
            return self.send("""<!doctype html><title>Video Calls</title>
<body style="font:22px sans-serif;margin:30px"><h1>Video Calls</h1>
<p><b>Your browser is not supported.</b> Please use Google Chrome or Microsoft Edge.</p>""")
        if u.path != "/":
            return self.send("not found", "text/plain", status=404)
        who = self.user()
        status = ("Signed in as " + html.escape(who)) if who else "Not signed in"
        self.send(PAGE.format(title="Example Account", status=status, bg="#e6f4ec" if who else "#fdeaea"))


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"serving on http://127.0.0.1:{PORT}/", flush=True)
    srv.serve_forever()
