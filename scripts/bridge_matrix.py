#!/usr/bin/env python3
"""What request-filtering extensions do to the same pages in Gecko and in
Blink (through the extension bridge). Results feed docs/extension-bridge.md.

  python3 scripts/deps.py fetch-test-addons
  xvfb-run -a -s "-screen 0 1280x1024x24" python3 scripts/bridge_matrix.py [--out FILE]

For each setup (no extension, uBlock Origin, ClearURLs, Privacy Badger) and
each engine (Gecko, Blink), a fresh browser opens the fixture pages of
tests/signin_site.py and records what reached the site and what the page
saw:
  ads         an ad script (stand-in served?) and a DoubleClick image
              (blocked?); needs network for the real files
  subresource tracking parameters on a third-party image (stripped?)
  navigation  tracking parameters on the page address itself (stripped?)
  headers     DNT / Sec-GPC on the page request (added?)
  websocket   a WebSocket connection (did the engine ask Firefox at all?)
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from bench import launch, poll, tab  # noqa: E402
from browserlib import free_port  # noqa: E402
from hostlib import Site  # noqa: E402

ADDONS = os.path.join(ROOT, "third_party/test-addons")
SETUPS = ["none", "ublock-origin", "clearurls", "privacy-badger"]


# The fixtures are served under non-local names: ClearURLs leaves local
# addresses (localhost, 127.0.0.1) alone by default. Both engines map *.test
# to this machine.
TEST_PREFS = 'user_pref("network.dns.localDomains", "fixtures.test,third.test");\n'
TEST_ENV = {"HYBRID_CHROMIUM_FLAGS": '--disable-gpu --host-resolver-rules="MAP *.test 127.0.0.1"'}


def url(site, path):
    return f"http://fixtures.test:{site.port}{path}"


def ready(b, site, setup):
    """Wait until the extension acts, judged in a Gecko tab so the wait
    doesn't depend on the bridge."""
    if setup == "none":
        return
    b.m.install_addon(os.path.join(ADDONS, setup + ".xpi"))
    poll(lambda: b.m.js("return HybridTest.host().bridgeActive"), 30, "bridge active")
    if setup == "clearurls":
        # ClearURLs downloads its rules after installing.
        def strips():
            site.reset()
            b.m.js("return HybridTest.openTab(arguments[0])", url(site, "/?utm_source=probe&id=1"))
            try:
                e = site.wait_for(lambda e: e["path"] == "/", 10)
            except AssertionError:
                return False
            return "utm_source" not in e["query"]
        poll(strips, 120, "ClearURLs rules")
    else:
        time.sleep(8)  # filter lists / tracker data load after installing


def open_and_wait(b, site, engine, path, report=None, timeout=30):
    site.reset()
    target = url(site, path)
    if engine == "gecko":
        b.m.js("return HybridTest.openTab(arguments[0])", target)
    else:
        b.m.js("return HybridTest.openTab(arguments[0], arguments[1])", target, engine)
    first = urllib.parse.urlparse(target).path
    try:
        site.wait_for(lambda e: e["path"] == first, timeout)
        if report:
            return site.report(report, timeout)
    except AssertionError:
        return None
    time.sleep(2)
    return True


def run(setup, engine, site):
    b = launch(f"matrix-{setup}-{engine}", TEST_ENV, TEST_PREFS)
    r = {}
    other = f"third.test:{site.port}"
    try:
        ready(b, site, setup)
        # ads (real network)
        r["ad_script_standin"] = open_and_wait(b, site, engine, "/ads-test", "standin", 40)
        try:
            r["doubleclick"] = site.report("doubleclick", 20)
        except AssertionError:
            r["doubleclick"] = None
        if engine != "gecko":
            t = b.m.js("return HybridTest.tabs().at(-1)")
            r["blink_blocked_log"] = t["blockedLog"]
        # tracking parameters on a third-party subresource
        open_and_wait(b, site, engine, f"/fx/clearurls?via={other}")
        pix = [e for e in site.log() if e["path"] == "/pixel.svg"]
        r["subresource_query"] = pix[0]["query"] if pix else None
        # tracking parameters on the page itself, and privacy headers
        open_and_wait(b, site, engine, "/?utm_source=newsletter&id=7")
        page = [e for e in site.log() if e["path"] == "/"]
        r["navigation_query"] = page[0]["query"] if page else None
        r["dnt"] = page[0]["dnt"] if page else None
        r["gpc"] = page[0]["gpc"] if page else None
        # websocket
        before = b.m.js("return HybridTest.host().classified")
        open_and_wait(b, site, engine, f"/fx/websocket?via={other}")
        time.sleep(1)
        after = b.m.js("return HybridTest.host().classified")
        r["websocket_reached_server"] = any(e["websocket"] for e in site.log())
        r["websocket_checks"] = sum(v for k, v in after.items() if "/ws" in k) - sum(
            v for k, v in before.items() if "/ws" in k)
    finally:
        b.quit()
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "docs/bridge-matrix.json"))
    ap.add_argument("--setups", nargs="*", default=SETUPS)
    args = ap.parse_args()
    from Xlib import display
    keeper = display.Display()  # keep the X server from resetting between launches
    site = Site(free_port())
    results = {}
    try:
        for setup in args.setups:
            if setup != "none" and not os.path.exists(os.path.join(ADDONS, setup + ".xpi")):
                print(f"skipping {setup}: run scripts/deps.py fetch-test-addons")
                continue
            for engine in ("gecko", "blink"):
                print(f"{setup:>15} / {engine}", flush=True)
                results[f"{setup}/{engine}"] = res = run(setup, engine, site)
                print("   ", {k: v for k, v in res.items() if k != "blink_blocked_log"}, flush=True)
    finally:
        site.stop()
        keeper.close()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)
    print("results:", args.out)


if __name__ == "__main__":
    main()
