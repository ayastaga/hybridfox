#!/usr/bin/env python3
"""Smoke test for chromium-host without Firefox.

Creates an X11 window that stands in for the browser (tab strip drawn as a
grey bar, content area below), launches chromium-host, and drives it with
the same protocol the Firefox layer uses. Saves screenshots along the way.

Usage: DISPLAY=:99 python3 test_host.py path/to/chromium-host outdir
"""
import json, os, subprocess, sys, threading, time
from Xlib import X, display, Xatom

host_path, outdir = sys.argv[1], sys.argv[2]
os.makedirs(outdir, exist_ok=True)

d = display.Display()
screen = d.screen()
W, H, TOP = 1000, 700, 80
# Like Firefox, use a non-default visual with its own colormap: Chromium's
# window must not inherit the parent's colormap (that failed silently once).
visual = screen.root_visual
for depth in screen.allowed_depths:
    if depth.depth != screen.root_depth:
        continue
    for v in depth.visuals:
        if v.visual_id != screen.root_visual and v.visual_class == X.TrueColor:
            visual = v.visual_id
            break
cmap = screen.root.create_colormap(visual, X.AllocNone)
print("parent visual", hex(visual), "default", hex(screen.root_visual), flush=True)
win = screen.root.create_window(0, 0, W, H, 0, screen.root_depth,
                                visual=visual, colormap=cmap, border_pixel=0,
                                background_pixel=0xdddddd,
                                event_mask=X.ExposureMask)
win.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [os.getpid()])
win.set_wm_name("fake firefox")
win.map()
gc = win.create_gc(foreground=0x888888)
win.fill_rectangle(gc, 0, 0, W, TOP)
d.sync()

events = []
cond = threading.Condition()

proc = subprocess.Popen([host_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1,
                        env={**os.environ, "HYBRID_NO_SANDBOX": "1",
                             "HYBRID_CHROMIUM_PROFILE": "/tmp/hybrid-test-profile"})

def reader():
    for line in proc.stdout:
        if line.startswith("@@"):
            ev = json.loads(line[2:])
            print("<-", ev, flush=True)
            with cond:
                events.append(ev)
                cond.notify_all()
threading.Thread(target=reader, daemon=True).start()

def send(**cmd):
    print("->", cmd, flush=True)
    proc.stdin.write(json.dumps(cmd) + "\n")
    proc.stdin.flush()

def wait_for(pred, timeout=30):
    end = time.time() + timeout
    with cond:
        while time.time() < end:
            for ev in events:
                if pred(ev):
                    return ev
            cond.wait(0.5)
    raise SystemExit(f"timeout waiting; events so far: {events[-5:]}")

def shot(name):
    d.sync()
    time.sleep(1.5)
    subprocess.run(["import", "-window", "root", os.path.join(outdir, name)], check=True)
    print("screenshot", name, flush=True)

PAGE1 = "data:text/html," + "<body style='margin:0;font:40px sans-serif;background:%231f9ec4;color:white'><h1 style='margin:40px'>Chromium tab 1</h1><p style='margin:40px' id=u></p><script>document.getElementById('u').textContent=navigator.userAgent.match(/Chrome\\/[\\d.]+/)[0]</script></body>"
PAGE2 = "data:text/html,<title>Second</title><body style='background:%23e8590c;color:white;font:40px sans-serif'><h1>Chromium tab 2</h1><a href='https://example.com' target=_blank>popup link</a></body>"

wait_for(lambda e: e["event"] == "ready")
send(cmd="attach", pid=os.getpid(), w=W, h=H)
wait_for(lambda e: e["event"] == "attached")
send(cmd="create", id=1, url=PAGE1, x=0, y=TOP, w=W, h=H - TOP, visible=True)
wait_for(lambda e: e["event"] == "created" and e["id"] == 1)
wait_for(lambda e: e["event"] == "loading" and e["id"] == 1 and not e["loading"])
shot("1-first-tab.png")

# Second tab created hidden, then switch to it (what TabSelect does).
send(cmd="create", id=2, url=PAGE2, x=0, y=TOP, w=W, h=H - TOP, visible=False)
wait_for(lambda e: e["event"] == "created" and e["id"] == 2)
wait_for(lambda e: e["event"] == "title" and e["id"] == 2)
send(cmd="hide", id=1)
send(cmd="show", id=2, x=0, y=TOP, w=W, h=H - TOP)
shot("2-switched-tab.png")

# Simulate a sidebar opening: content area shrinks from the left.
send(cmd="bounds", id=2, x=250, y=TOP, w=W - 250, h=H - TOP)
win.fill_rectangle(win.create_gc(foreground=0x444444), 0, TOP, 250, H - TOP)
shot("3-resized.png")

# Navigate tab 1 to a real site, then back.
send(cmd="hide", id=2)
send(cmd="show", id=1, x=0, y=TOP, w=W, h=H - TOP)
send(cmd="navigate", id=1, url="https://example.com/")
wait_for(lambda e: e["event"] == "url" and e["id"] == 1 and "example.com" in e["url"])
wait_for(lambda e: e["event"] == "loading" and e["id"] == 1 and not e["loading"] and e["canGoBack"], 40)
shot("4-navigated.png")

send(cmd="close", id=2)
wait_for(lambda e: e["event"] == "closed" and e["id"] == 2)
send(cmd="quit")
proc.wait(timeout=30)
print("host exited", proc.returncode)
print("ALL CHECKS PASSED")
