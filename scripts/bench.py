#!/usr/bin/env python3
"""Benchmarks for HybridFox. Prints medians and writes the raw samples as JSON.

  xvfb-run -a -s "-screen 0 1280x1024x24" python3 scripts/bench.py [--runs N] [--out FILE]
      [--sites URL ...] [--skip NAME ...]

Measured (each repeated --runs times; medians reported):
  switch    time to switch a loaded tab between each ordered pair of engines,
            from the switch command until the new engine has loaded the page
  cookies   cookie propagation: a cookie set by a page in Blink, until
            Firefox's jar has it, and until a page in WebKit can read it
  bridge    page load time of a page with 50 third-party images in a Blink
            tab: checks off (HYBRID_NO_PROTECTION=1), Firefox's checks only,
            and with uBlock Origin (every request asked through the bridge)
  sites     the same three setups on real sites (needs network)
  memory    proportional set size (PSS) of each engine's process tree, and
            of Firefox's, with one tab per engine open

Times come from one clock (this machine's): the test site's request log
stamps when an engine asked for something, and the browser is polled
through Marionette every 10 ms.
"""
import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from browserlib import Browser, free_port  # noqa: E402
from hostlib import HOSTS, Site  # noqa: E402

UBLOCK = os.path.join(ROOT, "third_party/test-addons/ublock-origin.xpi")
ENGINES = ["gecko", "blink", "webkit"]
DEFAULT_SITES = [
    "https://en.wikipedia.org/wiki/Web_browser",
    "https://www.theguardian.com/international",
    "https://www.bbc.com/news",
]
POLL = 0.01


def median(xs):
    return round(statistics.median(xs), 1) if xs else None


def spread(xs):
    """25th-75th percentile, to show how noisy a median is."""
    if len(xs) < 4:
        return ""
    q = statistics.quantiles(xs, n=4)
    return f"(p25-p75 {q[0]:.0f}-{q[2]:.0f})"


def poll(fn, timeout=60, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(POLL)
    raise TimeoutError(what)


def launch(name, env=None, prefs=""):
    return Browser(Path(tempfile.mkdtemp(prefix="hybrid-bench-")), "bench-" + name, env, prefs=prefs)


def tab(b, i):
    return b.m.js("return HybridTest.tabs()[arguments[0]]", i)


def wait_loaded_in(b, i, engine, loads_before=0, timeout=60):
    """Until tab |i| runs on |engine| and has finished a load there."""
    def done():
        t = tab(b, i)
        if t["engine"] != engine:
            return False
        if engine == "gecko":
            return not t["loading"] and t["url"].startswith("http")
        return (t["loads"] or 0) > loads_before
    return poll(done, timeout, f"tab {i} loaded in {engine}")


def ensure_ublock_ready(b, site):
    """uBlock Origin lets everything through until its lists are loaded."""
    b.m.install_addon(UBLOCK)
    poll(lambda: b.m.js("return HybridTest.host().bridgeActive"), 30, "bridge active")
    i = b.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    poll(lambda: tab(b, i)["identity"] == site.base, 30, "identity")
    def blocks():
        r = b.m.js_async("HybridTest.probe('https://ad.doubleclick.net/ddm/ad/x;sz=1x1', 4).then(resolve)")
        return r.get("block")
    poll(blocks, 90, "uBlock Origin ready")
    return i


# ------------------------------------------------------------------ switch --

def bench_switch(site, runs):
    b = launch("switch")
    out = {}
    try:
        i = b.m.js("return HybridTest.openTab(arguments[0])", site.url("/"))
        wait_loaded_in(b, i, "gecko")
        current = "gecko"
        for src in ENGINES:
            for dst in ENGINES:
                if src == dst:
                    continue
                samples = []
                for _ in range(runs):
                    if current != src:
                        b.m.js("HybridTest.switchEngine(arguments[0], null, arguments[1])", src, i)
                        wait_loaded_in(b, i, src)
                        current = src
                    time.sleep(0.3)
                    site.reset()
                    t0 = time.time()
                    b.m.js("HybridTest.switchEngine(arguments[0], null, arguments[1])", dst, i)
                    # The new engine must really fetch the page (not just
                    # report a stale "not loading"), then finish loading it.
                    site.wait_for(lambda e: e["path"] == "/" and e["engine"] == dst, 60)
                    wait_loaded_in(b, i, dst)
                    samples.append((time.time() - t0) * 1000)
                    current = dst
                out[f"{src}->{dst}"] = samples
                print(f"  switch {src:>6} -> {dst:<6} median {median(samples):7.1f} ms {spread(samples)}", flush=True)
    finally:
        b.quit()
    return out


# ----------------------------------------------------------------- cookies --

def bench_cookies(site, runs):
    b = launch("cookies")
    out = {"engine_to_firefox": [], "firefox_to_engine": [], "total": []}
    try:
        bl = b.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
        wk = b.m.js("return HybridTest.openTab(arguments[0], 'webkit')", site.url("/"))
        wait_loaded_in(b, bl, "blink")
        wait_loaded_in(b, wk, "webkit")
        for r in range(runs):
            v = f"{int(time.time())}x{r}"
            loads = tab(b, wk)["loads"]
            b.m.js("HybridTest.navigate(arguments[0], arguments[1])", wk, site.url(f"/cookiewatch?v={v}"))
            wait_loaded_in(b, wk, "webkit", loads)
            site.reset()
            # Stamp the moment Firefox's jar gets the cookie from inside
            # Firefox (polling over Marionette would add its own delay).
            b.m.js(COOKIE_STAMP, v)
            b.m.js("HybridTest.navigate(arguments[0], arguments[1])", bl, site.url(f"/setprobe?v={v}"))
            in_ff = poll(lambda: b.m.js("return window._hybridCookieAt"), 30, "cookie in Firefox") / 1000
            seen = site.wait_for(lambda e: e["path"] == "/_report" and f"probe%3D{v}" in e["query"], 30)
            issued = next(e for e in site.log() if e["path"] == "/setprobe")
            out["engine_to_firefox"].append((in_ff - issued["t"]) * 1000)
            out["firefox_to_engine"].append((seen["t"] - in_ff) * 1000)
            out["total"].append((seen["t"] - issued["t"]) * 1000)
        for k, xs in out.items():
            print(f"  cookies {k:<18} median {median(xs):7.1f} ms {spread(xs)}", flush=True)
    finally:
        b.quit()
    return out


COOKIE_STAMP = """
window._hybridCookieAt = null;
const want = arguments[0];
const obs = {
  observe(subject) {
    try {
      const c = subject.QueryInterface(Ci.nsICookieNotification).cookie;
      if (c && c.name === 'probe' && c.value === want) {
        window._hybridCookieAt = Date.now();
        Services.obs.removeObserver(obs, 'cookie-changed');
      }
    } catch (e) {}
  },
};
Services.obs.addObserver(obs, 'cookie-changed');
"""


# ------------------------------------------------------------------ bridge --

def load_times(b, i, urls, runs, timeout=60):
    """Navigate tab |i| (Blink) to each URL |runs| times; load times in ms."""
    res = {u: [] for u in urls}
    for _ in range(runs):
        for u in urls:
            loads = tab(b, i)["loads"]
            t0 = time.time()
            b.m.js("HybridTest.navigate(arguments[0], arguments[1])", i, u)
            try:
                wait_loaded_in(b, i, "blink", loads, timeout)
                res[u].append((time.time() - t0) * 1000)
            except TimeoutError:
                print(f"    timeout: {u}", flush=True)
            # Back to a blank, cheap page so the next load starts the same way.
            loads = tab(b, i)["loads"]
            b.m.js("HybridTest.navigate(arguments[0], 'about:blank')", i)
            try:
                wait_loaded_in(b, i, "blink", loads, 10)
            except TimeoutError:
                pass
    return res


SETUPS = [
    ("checks off", {"HYBRID_NO_PROTECTION": "1"}, False),
    ("Firefox checks", {}, False),
    ("uBlock Origin", {}, True),
]


def bench_bridge(site, runs, real_sites):
    out = {}
    many = lambda r: site.url(f"/many?n=50&via={site.other}&r={r}")  # noqa: E731
    for label, env, ublock in SETUPS:
        if ublock and not os.path.exists(UBLOCK):
            print("  (skipping uBlock Origin: run scripts/deps.py fetch-test-addons)")
            continue
        b = launch("bridge", env)
        try:
            if ublock:
                i = ensure_ublock_ready(b, site)
            else:
                i = b.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
                wait_loaded_in(b, i, "blink")
            load_times(b, i, [many("warm")], 2)  # warm up the engine
            fixture, spans = [], []
            for r in range(max(runs, 15)):
                site.reset()
                fixture += load_times(b, i, [many(r)], 1)[many(r)]
                # Server side: from the page request to the last image
                # request, without the load event's own jitter.
                log = site.log()
                page = [e["t"] for e in log if e["path"] == "/many"]
                pix = [e["t"] for e in log if e["path"] == "/pixel.svg"]
                if page and len(pix) == 50:
                    spans.append((max(pix) - page[0]) * 1000)
                elif not ublock:
                    print(f"    only {len(pix)}/50 images requested", flush=True)
            out[f"fixture/{label}"] = fixture
            out[f"fixture-span/{label}"] = spans
            print(f"  50 third-party images, {label:<15} load {median(fixture):7.1f} ms {spread(fixture)}; "
                  f"page->last image {median(spans) or float('nan'):7.1f} ms {spread(spans)}", flush=True)
            if real_sites:
                for u, xs in load_times(b, i, real_sites, runs).items():
                    out[f"{u}/{label}"] = xs
                    print(f"  {u:<45} {label:<15} median {median(xs) or float('nan'):7.1f} ms "
                          f"({len(xs)}/{runs} loaded)", flush=True)
        finally:
            b.quit()
    return out


# ------------------------------------------------------------------ memory --

def children_of():
    kids = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(pid))
    return kids


def tree(pid, kids):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p, [])
    return out


def pss_mb(pids):
    total = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/smaps_rollup") as f:
                for line in f:
                    if line.startswith("Pss:"):
                        total += int(line.split()[1])
        except OSError:
            pass
    return total / 1024


def bench_memory(site, runs):
    out = {"firefox": [], "blink": [], "webkit": []}
    for _ in range(runs):
        b = launch("memory")
        try:
            for engine in ("blink", "webkit"):
                i = b.m.js("return HybridTest.openTab(arguments[0], arguments[1])", site.url("/"), engine)
                wait_loaded_in(b, i, engine)
            time.sleep(3)  # let startup work settle
            st = b.m.js("return HybridTest.host()")["engines"]
            kids = children_of()
            engine_pids = {e: tree(st[e]["pid"], kids) for e in ("blink", "webkit")}
            ff = [p for p in tree(b.proc.pid, kids) if not any(p in v for v in engine_pids.values())]
            out["firefox"].append(pss_mb(ff))
            for e, pids in engine_pids.items():
                out[e].append(pss_mb(pids))
        finally:
            b.quit()
    for k, xs in out.items():
        print(f"  memory {k:<8} median PSS {median(xs):7.1f} MB", flush=True)
    return out


# -------------------------------------------------------------------- main --

def machine():
    info = {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count()}
    try:
        with open("/proc/cpuinfo") as f:
            model = [l.split(":", 1)[1].strip() for l in f if l.lower().startswith(("model name", "cpu part"))]
        info["cpu"] = model[0] if model else ""
        with open("/proc/meminfo") as f:
            info["mem_gb"] = round(int(f.readline().split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    with open(os.path.join(ROOT, "versions.json")) as f:
        v = json.load(f)
    info["firefox"], info["cef"] = v["firefox"]["version"], v["cef"]["version"]
    return info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=7)
    ap.add_argument("--out", default=os.path.join(ROOT, "docs/bench-results.json"))
    ap.add_argument("--sites", nargs="*", default=DEFAULT_SITES)
    ap.add_argument("--skip", nargs="*", default=[])
    args = ap.parse_args()
    if not os.environ.get("DISPLAY"):
        sys.exit("needs an X display: run under xvfb-run")
    for path in (HOSTS["blink"], HOSTS["webkit"]):
        if not os.access(path, os.X_OK):
            sys.exit(f"not built: {path}")
    from Xlib import display  # keep the X server from resetting between launches
    keeper = display.Display()
    site = Site(free_port())
    results = {"machine": machine(), "runs": args.runs, "when": time.strftime("%Y-%m-%d %H:%M %Z")}
    try:
        if "switch" not in args.skip:
            print("Engine switch (loaded tab)", flush=True)
            results["switch"] = bench_switch(site, args.runs)
        if "cookies" not in args.skip:
            print("Cookie propagation (Blink -> Firefox -> WebKit)", flush=True)
            results["cookies"] = bench_cookies(site, args.runs)
        if "bridge" not in args.skip:
            print("Extension bridge and request checks (Blink)", flush=True)
            results["bridge"] = bench_bridge(site, args.runs, [] if "sites" in args.skip else args.sites)
        if "memory" not in args.skip:
            print("Memory (PSS)", flush=True)
            results["memory"] = bench_memory(site, max(3, args.runs // 2))
    finally:
        site.stop()
        keeper.close()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)
    print("raw samples:", args.out)


if __name__ == "__main__":
    main()
