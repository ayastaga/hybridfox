"""The real browser (./hybrid), driven through Marionette in the chrome context.

Each test launches HybridFox with a fresh, temporary profile (XDG_DATA_HOME),
HYBRID_TEST=1 (which installs window.HybridTest, see hybrid-browser.js) and
--marionette, then checks behaviour through HybridTest, HybridHost's debug
state and the test site's request log.
"""
import os
import signal
import subprocess
import time

import pytest

from conftest import ARTIFACTS, free_port
from hostlib import HOSTS, ROOT
from marionette import Marionette

FIREFOX = os.path.join(ROOT, "third_party/firefox/firefox")
PROFILE_DIRNAME = "hybrid-browser"

PREFS = """
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.startup.page", 0);
user_pref("startup.homepage_welcome_url", "");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.tabs.warnOnClose", false);
user_pref("devtools.console.stdout.chrome", true);
user_pref("browser.dom.window.dump.enabled", true);
user_pref("marionette.port", {port});
"""


class Browser:
    def __init__(self, tmp_path, name, env=None):
        self.data = tmp_path / "data"
        profile = self.data / PROFILE_DIRNAME / "firefox-profile"
        profile.mkdir(parents=True)
        port = free_port()
        (profile / "user.js").write_text(PREFS.replace("{port}", str(port)))
        self.log_path = os.path.join(ARTIFACTS, f"{name}-browser.log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [os.path.join(ROOT, "hybrid"), "--marionette", "about:blank"],
            env={
                **os.environ,
                "XDG_DATA_HOME": str(self.data),
                "HYBRID_TEST": "1",
                # Firefox only lets Marionette into the chrome context with this.
                "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1",
                "HYBRID_DEBUG": "1",
                "HYBRID_NO_SANDBOX": "1",
                "HYBRID_CHROMIUM_FLAGS": os.environ.get("HYBRID_CHROMIUM_FLAGS", "--disable-gpu"),
                **(env or {}),
            },
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.m = Marionette(port)
        self.m.start()
        self.m.wait("return !!window.HybridTest", what="the hybrid layer to load")

    # Shorthands for window.HybridTest
    def tabs(self):
        return self.m.js("return HybridTest.tabs()")

    def tab(self, index):
        return self.tabs()[index]

    def host(self):
        return self.m.js("return HybridTest.host()")

    def wait_tab(self, index, pred, timeout=30, what="tab state"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            tabs = self.tabs()
            last = tabs[index] if index < len(tabs) else None
            if last and pred(last):
                return last
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}; tab {index}: {last}")

    def log_text(self):
        self.log.flush()
        with open(self.log_path) as f:
            return f.read()

    def quit(self):
        try:
            self.m.js("Services.startup.quit(Ci.nsIAppStartup.eForceQuit)")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # Only this test's process group: never match by name.
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.log.close()


@pytest.fixture
def browser(request, tmp_path, site):
    for path in (FIREFOX, HOSTS["blink"]):
        if not os.access(path, os.X_OK):
            pytest.skip(f"not built: {path}")
    b = Browser(tmp_path, request.node.name)
    site.reset()
    yield b
    b.quit()


def needs_webkit():
    if not os.access(HOSTS["webkit"], os.X_OK):
        pytest.skip("webkit host isn't built")


# ------------------------------------------------- extension-bridge plumbing --

def test_placeholder_takes_site_identity(browser, site):
    """The blank page under an engine tab carries the site's identity, even
    when its own about:blank load finishes after the identity was applied."""
    i = browser.m.js("return HybridTest.openTab(arguments[0])", site.url("/"))
    browser.wait_tab(i, lambda t: t["url"].startswith(site.base) and not t["loading"], what="Gecko load")
    browser.m.js("HybridTest.switchEngine('blink')")
    browser.wait_tab(i, lambda t: t["engine"] == "blink" and t["identity"] == site.base, what="identity")
    # The race: the placeholder's own about:blank load lands after the
    # identity was applied. Timing decides whether it happens on its own, so
    # make it happen: load a plain about:blank into the placeholder now.
    browser.m.js("""
      const b = gBrowser.tabs[arguments[0]].linkedBrowser;
      b.loadURI(Services.io.newURI('about:blank'), {
        triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
      });""", i)
    time.sleep(2)  # the blank load lands, then the identity must be back
    t = browser.tab(i)
    assert t["identity"] == site.base, f"placeholder lost the site's identity: {t}"
    assert t["reapplied"] == 1, f"re-applied {t['reapplied']} times (looping?)"


def test_failed_probe_is_no_verdict(browser, site):
    i = browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    browser.wait_tab(i, lambda t: t["identity"] == site.base, what="Blink tab with identity")
    # A URL no channel can be made for: the probe must not answer "allow".
    r = browser.m.js_async("HybridTest.probe('http://[::1', 4).then(resolve)")
    assert r.get("noVerdict") is True and not r.get("allow"), r


# ----------------------------------------------------------------- cookies --

ADD_COOKIE = """
const [host, name, value, oa] = arguments;
const ms = Date.now() + 3600 * 1000;  // Firefox 159 stores expiry in ms
Services.cookies.add(host, '/', name, value, false, true, false, ms, oa,
  Ci.nsICookie.SAMESITE_LAX, Ci.nsICookie.SCHEME_HTTP, false);
"""


def test_isolated_cookies_stay_in_firefox(browser, site):
    """Container and partitioned cookies are deliberately not mirrored."""
    browser.m.js(ADD_COOKIE, "127.0.0.1", "session", "container", {"userContextId": 1})
    browser.m.js(ADD_COOKIE, "127.0.0.1", "session", "partitioned",
                 {"partitionKey": "(http,example.org)"})
    browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="Blink page view")
    assert view["user"] is None, view
    # Control: the same cookie in Firefox's plain jar does reach the engine.
    browser.m.js(ADD_COOKIE, "127.0.0.1", "session", "plain", {})
    time.sleep(1)
    site.reset()
    browser.m.js("BrowserCommands.reload()")
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="reloaded view")
    assert view["user"] == "plain", view


# --------------------------------------------------------- crash recovery --

def blink_state(browser):
    return browser.host()["engines"]["blink"]


def kill_blink(browser):
    """Kill the Blink host like a crash would; return its pid."""
    pid = blink_state(browser)["pid"]
    assert pid
    os.kill(pid, signal.SIGKILL)
    return pid


def wait_new_blink(browser, old_pid, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = blink_state(browser)
        if st["ready"] and st["pid"] and st["pid"] != old_pid:
            return st
        time.sleep(0.25)
    raise AssertionError(f"Blink host wasn't restarted: {blink_state(browser)}")


def test_engine_crash_reopens_tabs(browser, site):
    # Signed in inside the engine before the crash.
    i = browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/login?u=alice"))
    site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink" and e["user"] == "alice")
    before = browser.wait_tab(i, lambda t: t["engine"] == "blink" and not t["loading"], what="Blink tab")
    time.sleep(1.5)  # the engine reports its cookies to Firefox once a second
    site.reset()
    old = kill_blink(browser)
    st = wait_new_blink(browser, old)
    assert st["restarts"] == 1 and st["failure"] is None
    # Reopened in the new process: same tab id, same address, loaded again.
    # ...and still signed in: the new process got Firefox's cookie jar.
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="tab reloaded after the crash")
    assert view["user"] == "alice", view
    after = browser.wait_tab(i, lambda t: not t["loading"], what="reopened tab")
    assert after["engine"] == "blink" and after["id"] == before["id"] and after["url"] == before["url"]
    assert not after["dead"]
    assert "stopped" not in browser.m.js("return HybridTest.engineButton().label")


def test_engine_gives_up_after_three_crashes(browser, site):
    i = browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    browser.wait_tab(i, lambda t: t["engine"] == "blink" and not t["loading"], what="Blink tab")
    pid = kill_blink(browser)
    pid = wait_new_blink(browser, pid)["pid"]
    os.kill(pid, signal.SIGKILL)
    pid = wait_new_blink(browser, pid)["pid"]
    os.kill(pid, signal.SIGKILL)  # third crash within two minutes
    deadline = time.time() + 15
    while time.time() < deadline and not blink_state(browser)["failure"]:
        time.sleep(0.25)
    st = blink_state(browser)
    assert st["failure"] and "3 times" in st["failure"], st
    time.sleep(2)
    assert not blink_state(browser)["running"]  # not restarted again
    assert browser.tab(i)["dead"]
    button = browser.m.js("return HybridTest.engineButton()")
    assert button["failed"] and button["label"] == "Blink · stopped", button


# ------------------------------------------------- sign-ins across engines --

def views(site, engine):
    return [e for e in site.log() if e["path"] == "/" and e["engine"] == engine]


def test_one_tab_through_three_engines_stays_signed_in(browser, site):
    needs_webkit()
    i = browser.m.js("return HybridTest.openTab(arguments[0])", site.url("/login?u=alice"))
    for engine in ("gecko", "blink", "webkit"):
        if engine != "gecko":
            site.reset()
            browser.m.js("HybridTest.switchEngine(arguments[0], null, arguments[1])", engine, i)
        view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == engine, what=f"{engine} view")
        assert view["user"] == "alice", (engine, view)
        browser.wait_tab(i, lambda t: t["engine"] == engine and not t["loading"], what=f"{engine} tab")


def test_webkit_sign_in_reaches_new_blink_tab(browser, site):
    needs_webkit()
    browser.m.js("return HybridTest.openTab(arguments[0], 'webkit')", site.url("/login?u=wendy"))
    site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "webkit" and e["user"] == "wendy")
    # WebKit -> Firefox: polled once a second.
    browser.m.wait("""return Services.cookies.getCookiesFromHost('127.0.0.1', {})
                        .some(c => c.name === 'session' && c.value === 'wendy')""", what="cookie in Firefox")
    browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="Blink view")
    assert view["user"] == "wendy"


def test_live_sign_out_relays_from_blink_to_webkit(browser, site):
    needs_webkit()
    browser.m.js(ADD_COOKIE, "127.0.0.1", "session", "alice", {})
    b = browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    w = browser.m.js("return HybridTest.openTab(arguments[0], 'webkit')", site.url("/"))
    site.wait_for(lambda e: e["engine"] == "blink" and e["user"] == "alice", what="Blink signed in")
    site.wait_for(lambda e: e["engine"] == "webkit" and e["user"] == "alice", what="WebKit signed in")
    # Sign out inside the Blink tab while the WebKit tab stays open.
    browser.m.js("HybridTest.navigate(arguments[0], arguments[1])", b, site.url("/logout"))
    browser.m.wait("""return !Services.cookies.getCookiesFromHost('127.0.0.1', {})
                        .some(c => c.name === 'session')""", what="cookie gone from Firefox")
    time.sleep(1)  # Firefox -> WebKit is pushed at once; allow for delivery
    site.reset()
    browser.m.js("HybridTest.reload(arguments[0])", w)
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "webkit", what="WebKit reload")
    assert view["user"] is None, view


def test_engine_cookie_expiry_units(browser, site):
    """Firefox 159 stores expiry in milliseconds; a cookie from an engine
    (Max-Age=3600) must land about an hour from now, not in 1970 or 50000."""
    browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/login?u=alice"))
    expiry = browser.m.wait("""const c = Services.cookies.getCookiesFromHost('127.0.0.1', {})
                                 .find(c => c.name === 'session' && c.value === 'alice');
                               return c && c.expiry""", what="engine cookie in Firefox")
    secs = expiry / 1000 if expiry > 1e11 else expiry
    assert abs(secs - (time.time() + 3600)) < 300, expiry


# ------------------------------------------------------------ private windows --

OPEN_PRIVATE = """
window._hybridPrivate = OpenBrowserWindow({ private: true });
"""
IN_PRIVATE = "const P = window._hybridPrivate; "


def test_private_window_isolation_and_discard(browser, site):
    browser.m.js(ADD_COOKIE, "127.0.0.1", "session", "alice", {})
    browser.m.js(OPEN_PRIVATE)
    browser.m.wait(IN_PRIVATE + "return P.HybridTest && P.HybridTest.isPrivate", what="private window layer")
    browser.m.js(IN_PRIVATE + "P.HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="private Blink view")
    assert view["user"] is None  # normal sign-ins don't reach private windows
    # Sign in privately; it must not leak into normal tabs.
    browser.m.js(IN_PRIVATE + "P.HybridTest.navigate(null, arguments[0])", site.url("/login?u=pat"))
    site.wait_for(lambda e: e["path"] == "/" and e["user"] == "pat", what="private sign-in")
    assert browser.m.js("return HybridTest.rules()") == {}
    browser.m.js(IN_PRIVATE + "P.HybridTest.setRule('127.0.0.1', 'blink')")
    assert browser.m.js("return HybridTest.rules()") == {}  # no rules from private windows
    site.reset()
    browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    assert site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink")["user"] == "alice"
    # Last private window closes: the private session ends everywhere.
    browser.m.js(IN_PRIVATE + "P.close()")
    browser.m.wait("return !HybridTest.host().engines.blink.jars.private.started", what="private jar ended")
    assert "private session ended" in browser.log_text()
    # A new private window starts empty.
    browser.m.js(OPEN_PRIVATE)
    browser.m.wait(IN_PRIVATE + "return P.HybridTest && P.HybridTest.isPrivate", what="second private window")
    site.reset()
    browser.m.js(IN_PRIVATE + "P.HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "blink", what="new private view")
    assert view["user"] is None, view  # "pat" was discarded with the session


# ---------------------------------------------------------------- site rules --

def test_site_rule_opens_site_in_its_engine(browser, site):
    needs_webkit()
    browser.m.js("HybridTest.setRule('127.0.0.1', 'webkit')")
    assert browser.m.js("return HybridTest.rules()") == {"127.0.0.1": "webkit"}
    i = browser.m.js("return HybridTest.openTab(arguments[0])", site.url("/"))  # a Gecko load
    browser.wait_tab(i, lambda t: t["engine"] == "webkit", what="rule switching the tab to WebKit")
    view = site.wait_for(lambda e: e["path"] == "/" and e["engine"] == "webkit", what="WebKit view")
    assert view
    # Choosing Gecko for the site in this tab sticks (no bounce back).
    browser.m.js("HybridTest.switchEngine('gecko', null, arguments[0])", i)
    browser.wait_tab(i, lambda t: t["engine"] == "gecko" and not t["loading"], what="back to Gecko")
    time.sleep(1.5)
    assert browser.tab(i)["engine"] == "gecko"
    browser.m.js("HybridTest.setRule('127.0.0.1', null)")
    assert browser.m.js("return HybridTest.rules()") == {}


# ------------------------------------------------------------ uBlock Origin --

UBLOCK = os.path.join(ROOT, "third_party/test-addons/ublock-origin.xpi")


def test_ublock_origin_filters_blink_tab(browser, site):
    """uBlock Origin, a real Firefox extension, decides a Blink tab's
    requests through the extension bridge: its stand-in for the ad script
    is served, and DoubleClick is blocked."""
    if not os.path.exists(UBLOCK):
        pytest.skip("run: scripts/deps.py fetch-test-addons")
    browser.m.install_addon(UBLOCK)
    browser.m.wait("return HybridTest.host().bridgeActive", what="bridge to notice the extension")
    i = browser.m.js("return HybridTest.openTab(arguments[0], 'blink')", site.url("/"))
    browser.wait_tab(i, lambda t: t["identity"] == site.base, what="Blink tab with identity")
    # uBlock Origin loads its filter lists after installing; until then it
    # lets everything through. Ask through the bridge until it blocks.
    deadline = time.time() + 60
    verdict = None
    while time.time() < deadline:
        verdict = browser.m.js_async(
            "HybridTest.probe('https://ad.doubleclick.net/ddm/ad/probe;sz=1x1', 4).then(resolve)")
        if verdict.get("block"):
            break
        time.sleep(0.5)
    assert verdict and verdict.get("block"), f"uBlock Origin never blocked through the bridge: {verdict}"
    browser.m.js("HybridTest.navigate(arguments[0], arguments[1])", i, site.url("/ads-test"))
    standin = site.report("standin", timeout=40)
    assert standin == "true", "uBlock Origin's stand-in for adsbygoogle.js wasn't served"
    t = browser.wait_tab(i, lambda t: any("doubleclick.net" in b["url"] for b in t["blockedLog"]),
                         what="DoubleClick blocked")
    dc = [b for b in t["blockedLog"] if "doubleclick.net" in b["url"]]
    assert dc[0]["reason"] == "extension", t["blockedLog"]
    # The real adsbygoogle.js would also define adsbygoogle.loaded, so check
    # the engine served a body uBlock Origin substituted, not the real file.
    ads = [b for b in t["blockedLog"] if "adsbygoogle.js" in b["url"]]
    assert ads and ads[0]["reason"] == "replaced by an extension", t["blockedLog"]
