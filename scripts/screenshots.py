#!/usr/bin/env python3
"""Screenshots and the demo GIF in docs/screenshots/, from a scripted run.

  python3 scripts/deps.py fetch-test-addons
  xvfb-run -a -s "-screen 0 1280x800x24" python3 scripts/screenshots.py

Pictures of what the test suite checks, not a replacement for it:
  engine-gecko.png, engine-blink.png, engine-webkit.png
      one tab, signed in, in each engine
  ublock-blink.png   uBlock Origin's stand-in and block, in a Blink tab
  private-window.png a Blink tab in a private window (not signed in)
  demo.gif           one tab through Gecko -> Blink -> WebKit, still signed in
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from browserlib import Browser, free_port  # noqa: E402
from hostlib import Site  # noqa: E402
from PIL import Image  # noqa: E402
from Xlib import X, display  # noqa: E402

OUT = os.path.join(ROOT, "docs/screenshots")
UBLOCK = os.path.join(ROOT, "third_party/test-addons/ublock-origin.xpi")
W, H = 1280, 800


def grab(d):
    root = d.screen().root
    raw = root.get_image(0, 0, W, H, X.ZPixmap, 0xFFFFFFFF).data
    return Image.frombytes("RGB", (W, H), raw, "raw", "BGRX")


def place(win_js, b):
    # No window manager under Xvfb: put the window where the camera is.
    b.m.js(win_js + ".moveTo(0, 0); " + win_js + f".resizeTo({W}, {H});")


def settle(b, i, engine, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = b.m.js("return HybridTest.tabs()[arguments[0]]", i)
        if t["engine"] == engine and not t["loading"]:
            break
        time.sleep(0.1)
    time.sleep(1.2)  # let the page paint


def main():
    os.makedirs(OUT, exist_ok=True)
    d = display.Display()
    site = Site(free_port())
    b = Browser(Path(tempfile.mkdtemp(prefix="hybrid-shots-")), "screenshots")
    try:
        place("window", b)
        frames = []

        def frame(hold):
            img = grab(d).resize((W // 2, H // 2), Image.LANCZOS)
            frames.append((img, hold))

        # One tab through three engines, signed in throughout.
        i = b.m.js("return HybridTest.openTab(arguments[0])", site.url("/login?u=alice"))
        b.m.js("gBrowser.removeTab(gBrowser.tabs[0])")  # the empty start tab
        i = 0
        settle(b, i, "gecko")
        for engine in ("gecko", "blink", "webkit"):
            if engine != "gecko":
                b.m.js("HybridTest.switchEngine(arguments[0], null, arguments[1])", engine, i)
                frame(400)  # mid-switch
                settle(b, i, engine)
            grab(d).save(os.path.join(OUT, f"engine-{engine}.png"))
            frame(1800)
        frames[0][0].save(os.path.join(OUT, "demo.gif"), save_all=True, loop=0,
                          append_images=[f for f, _ in frames[1:]], duration=[h for _, h in frames],
                          optimize=True)

        # A private window: same site, its own (empty) sign-in.
        b.m.js("window._pw = OpenBrowserWindow({ private: true });")
        deadline = time.time() + 20
        while not b.m.js("return !!(window._pw && window._pw.HybridTest)") and time.time() < deadline:
            time.sleep(0.2)
        place("window._pw", b)
        b.m.js("window._pw.HybridTest.openTab(arguments[0], 'blink'); window._pw.gBrowser.removeTab(window._pw.gBrowser.tabs[0]);",
               site.url("/"))
        time.sleep(4)
        grab(d).save(os.path.join(OUT, "private-window.png"))
        b.m.js("window._pw.close()")
        time.sleep(1)

        # uBlock Origin deciding a Blink tab's requests.
        if os.path.exists(UBLOCK):
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            from bench import ensure_ublock_ready
            j = ensure_ublock_ready(b, site)
            b.m.js("HybridTest.selectTab(arguments[0])", j)
            b.m.js("HybridTest.navigate(arguments[0], arguments[1])", j, site.url("/ads-test"))
            site.report("standin", 40)
            time.sleep(2)
            grab(d).save(os.path.join(OUT, "ublock-blink.png"))
        else:
            print("skipping ublock-blink.png: run scripts/deps.py fetch-test-addons")
    finally:
        b.quit()
        site.stop()
        d.close()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
