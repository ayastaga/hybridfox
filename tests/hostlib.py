"""Helpers for driving an engine host (chromium-host, webkit-host) directly.

The tests stand in for Firefox: they make an X11 window shaped the way
Firefox's is (non-default visual, a 1x1 input-only focus proxy, a title
marker), start the engine host, speak its line protocol, click with XTEST
and read pixels back from the X server.
"""
import base64
import json
import os
import subprocess
import threading
import time
import urllib.request

from Xlib import X, Xatom, display
from Xlib.ext import xtest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS = {
    "blink": os.environ.get("HYBRID_HOST") or os.path.join(ROOT, "engine/build/out/chromium-host"),
    "webkit": os.environ.get("HYBRID_WEBKIT_HOST") or os.path.join(ROOT, "engine/build/webkit/webkit-host"),
}

W, H, TOP = 1000, 700, 80  # stand-in window, and where its "content area" starts


class Parent:
    """A stand-in Firefox window.

    Firefox's toplevel uses a non-default visual (32-bit ARGB) with its own
    colormap; an engine view that inherits it fails silently (BadMatch, blank
    view). So this window does the same, to keep that regression covered.
    """

    def __init__(self, marker):
        self.d = display.Display()
        screen = self.d.screen()
        self.screen = screen
        depth, visual = self._non_default_visual(screen)
        self.visual = visual
        cmap = screen.root.create_colormap(visual, X.AllocNone)
        self.win = screen.root.create_window(
            0, 0, W, H, 0, depth, X.InputOutput, visual,
            colormap=cmap, border_pixel=0, background_pixel=0xFFDDDDDD,
        )
        self.set_title(marker)
        self.win.map()
        # GTK's hidden focus proxy: keyboard focus goes through it.
        self.proxy = self.win.create_window(-1, -1, 1, 1, 0, 0, X.InputOnly, X.CopyFromParent)
        self.proxy.map()
        self.d.sync()

    @staticmethod
    def _non_default_visual(screen):
        default = screen.root_visual
        # Prefer what Firefox uses: a 32-bit TrueColor (ARGB) visual.
        for want_depth in (32, screen.root_depth):
            for depth in screen.allowed_depths:
                if depth.depth != want_depth:
                    continue
                for v in depth.visuals:
                    if v.visual_id != default and v.visual_class == X.TrueColor:
                        return depth.depth, v.visual_id
        raise RuntimeError("the X server offers no non-default TrueColor visual")

    def set_title(self, title):
        self.win.set_wm_name(title)
        self.win.change_property(
            self.d.intern_atom("_NET_WM_NAME"), self.d.intern_atom("UTF8_STRING"), 8, title.encode()
        )
        self.d.sync()

    def pixel(self, x, y):
        """(r, g, b) at window coordinates, read back from the X server."""
        raw = self.screen.root.get_image(x, y, 1, 1, X.ZPixmap, 0xFFFFFFFF).data
        b, g, r = raw[0], raw[1], raw[2]
        return r, g, b

    def row(self, y, x0, x1):
        raw = self.screen.root.get_image(x0, y, x1 - x0, 1, X.ZPixmap, 0xFFFFFFFF).data
        return [(raw[i + 2], raw[i + 1], raw[i]) for i in range(0, len(raw), 4)]

    def click(self, x, y, hold=0.15):
        # The hosts notice clicks by polling the pointer every 30 ms, so hold
        # the button long enough for a poll to see it, like a person would.
        xtest.fake_input(self.d, X.MotionNotify, x=x, y=y)
        self.d.sync()
        time.sleep(0.05)
        xtest.fake_input(self.d, X.ButtonPress, 1)
        self.d.sync()
        time.sleep(hold)
        xtest.fake_input(self.d, X.ButtonRelease, 1)
        self.d.sync()

    def close(self):
        try:
            self.win.destroy()
            self.d.sync()
            self.d.close()
        except Exception:
            pass


class Host:
    """One engine host process and its event stream."""

    def __init__(self, engine, log_path, env=None):
        self.engine = engine
        self.events = []
        self.cond = threading.Condition()
        self.out_lock = threading.Lock()
        # classify requests: return a reply dict, or None to leave it
        # unanswered. Default: allow everything.
        self.on_classify = lambda ev: {}
        full_env = {
            **os.environ,
            "HYBRID_NO_SANDBOX": "1",
            # Xvfb has no GPU; software rendering keeps pixels deterministic.
            "HYBRID_CHROMIUM_FLAGS": os.environ.get("HYBRID_CHROMIUM_FLAGS", "--disable-gpu"),
            **(env or {}),
        }
        self.log = open(log_path, "w")
        self.proc = subprocess.Popen(
            [HOSTS[engine]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            env=full_env, text=True, bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            if not line.startswith("@@"):
                self.log.write(line)
                continue
            try:
                ev = json.loads(line[2:])
            except ValueError:
                continue
            self.log.write("<- " + json.dumps(ev) + "\n")
            self.log.flush()
            if ev.get("event") == "classify":
                reply = self.on_classify(ev)
                if reply is not None:
                    self.send(cmd="classified", req=ev["req"], **reply)
            with self.cond:
                self.events.append(ev)
                self.cond.notify_all()

    def send(self, **cmd):
        with self.out_lock:
            self.log.write("-> " + json.dumps(cmd) + "\n")
            self.proc.stdin.write(json.dumps(cmd) + "\n")
            self.proc.stdin.flush()

    def wait(self, pred, timeout=20, what="event"):
        end = time.time() + timeout
        with self.cond:
            while True:
                for ev in self.events:
                    if pred(ev):
                        return ev
                left = end - time.time()
                if left <= 0:
                    raise AssertionError(f"timed out waiting for {what}; last events: {self.events[-8:]}")
                self.cond.wait(min(left, 0.5))

    def wait_event(self, name, timeout=20, **match):
        return self.wait(
            lambda e: e.get("event") == name and all(e.get(k) == v for k, v in match.items()),
            timeout, f"{name} {match}",
        )

    def mark(self):
        """Forget events seen so far (wait() then only sees new ones)."""
        with self.cond:
            self.events.clear()

    def attach(self, win_id, marker):
        self.send(cmd="attach", win=win_id, marker=marker)
        return self.wait_event("attached", win=win_id)

    def create(self, tab_id, url, win=1, jar="normal", visible=True, wait_loaded=True):
        self.send(cmd="create", id=tab_id, win=win, jar=jar, url=url, visible=visible,
                  x=0, y=TOP, w=W, h=H - TOP)
        self.wait_event("created", id=tab_id)
        if wait_loaded:
            self.wait_loaded(tab_id)

    def wait_loaded(self, tab_id, timeout=30):
        return self.wait(lambda e: e.get("event") == "loading" and e.get("id") == tab_id and not e["loading"],
                         timeout, f"tab {tab_id} to finish loading")

    def cookies(self, jar="normal"):
        self.mark()
        self.send(cmd="cookies-dump", jar=jar)
        return self.wait_event("cookies", jar=jar)["cookies"]

    def quit(self, timeout=30):
        if self.proc.poll() is None:
            try:
                self.send(cmd="quit")
            except (BrokenPipeError, ValueError):
                pass
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()
        finally:
            self.log.close()


class Site:
    """tests/signin_site.py on a free port, with its request log."""

    def __init__(self, port):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        # Another origin on the same server, for third-party requests.
        self.other = f"localhost:{port}"
        self.proc = subprocess.Popen(
            ["python3", os.path.join(ROOT, "tests/signin_site.py"), str(port)],
            stdout=subprocess.PIPE, text=True,
        )
        self.proc.stdout.readline()  # "serving on ..."

    def url(self, path):
        return self.base + path

    def log(self):
        with urllib.request.urlopen(self.base + "/_log", timeout=5) as r:
            return json.loads(r.read())

    def reset(self):
        urllib.request.urlopen(self.base + "/_reset", timeout=5).read()

    def wait_for(self, pred, timeout=20, what="request"):
        end = time.time() + timeout
        while time.time() < end:
            for entry in self.log():
                if pred(entry):
                    return entry
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {what}; site log: {self.log()[-10:]}")

    def report(self, key, timeout=20):
        """Value a page reported through /_report?key=value."""
        import urllib.parse
        entry = self.wait_for(
            lambda e: e["path"] == "/_report" and key in urllib.parse.parse_qs(e["query"]),
            timeout, f"report {key}",
        )
        return urllib.parse.parse_qs(entry["query"])[key][0]

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


def b64(s):
    return base64.b64encode(s.encode()).decode()
