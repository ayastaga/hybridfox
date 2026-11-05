"""Engine hosts driven directly through their line protocol, without Firefox.

Each test starts a fresh host attached to a stand-in Firefox window (see
hostlib.Parent) and checks results through the test site's request log or
pixels read back from the X server, not screenshots.
"""
import math
import time

import pytest

from hostlib import H, TOP, W, b64

ALICE = {"domain": "127.0.0.1", "path": "/", "name": "session", "value": "alice", "secure": False,
         "httpOnly": True, "session": False, "expires": time.time() + 3600, "sameSite": "lax"}


def close_to(rgb, want, tol=12):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


def page_views(site, path="/", engine=None):
    return [e for e in site.log() if e["path"] == path and (engine is None or e["engine"] == engine)]


# ------------------------------------------------------------ both engines --

def test_start_and_attach(engine, start_host, parent):
    h = start_host(engine)  # waits for "ready" and "attached"
    attached = next(e for e in h.events if e["event"] == "attached")
    assert int(attached["xid"]) == parent.win.id
    # The 1x1 input-only child is GTK's focus proxy; keyboard focus must go
    # through it, not the toplevel.
    assert attached["focusProxy"] is True


def test_renders_non_blank(engine, start_host, parent, site):
    h = start_host(engine)
    h.create(1, site.url("/"))
    # The signed-out account page is pink (#fdeaea). A blank view would be
    # the container's white, or the stand-in window's grey.
    deadline = time.time() + 10
    while not close_to(parent.pixel(20, TOP + 300), (0xFD, 0xEA, 0xEA)) and time.time() < deadline:
        time.sleep(0.3)
    assert close_to(parent.pixel(20, TOP + 300), (0xFD, 0xEA, 0xEA)), parent.pixel(20, TOP + 300)


def test_firefox_cookies_sign_page_in(engine, start_host, site):
    h = start_host(engine)
    h.send(cmd="cookies-replace", jar="normal", cookies=[ALICE])
    h.wait_event("cookies-ready", jar="normal")
    h.create(1, site.url("/"))
    view = site.wait_for(lambda e: e["path"] == "/", what="page view")
    assert view["engine"] == engine
    assert view["user"] == "alice"


def test_sign_in_and_out_are_reported(engine, start_host, site):
    h = start_host(engine)
    h.send(cmd="cookies-replace", jar="normal", cookies=[])
    h.wait_event("cookies-ready", jar="normal")
    h.create(1, site.url("/"))
    h.mark()
    h.send(cmd="navigate", id=1, url=site.url("/login?u=bob"))
    ev = h.wait(lambda e: e["event"] == "cookie-set" and e["name"] == "session", 10, "cookie-set")
    assert ev["value"] == "bob" and ev["jar"] == "normal" and ev["httpOnly"] is True
    assert ev["domain"].lstrip(".") == "127.0.0.1"
    h.mark()
    h.send(cmd="navigate", id=1, url=site.url("/logout"))
    ev = h.wait(lambda e: e["event"] == "cookie-del" and e["name"] == "session", 10, "cookie-del")
    assert ev["jar"] == "normal"


@pytest.mark.xfail(reason="needs cookies-dump and SameSite comparison (work item 2)", strict=True)
def test_samesite_only_change_is_applied(engine, start_host):
    h = start_host(engine)
    h.send(cmd="cookies-replace", jar="normal", cookies=[ALICE])
    h.wait_event("cookies-ready", jar="normal")
    assert [c["sameSite"] for c in h.cookies() if c["name"] == "session"] == ["lax"]
    # Same name, value, flags and expiry; only SameSite differs.
    h.send(cmd="cookie-set", jar="normal", **{**ALICE, "sameSite": "strict"})
    deadline = time.time() + 5
    while time.time() < deadline:
        got = [c for c in h.cookies() if c["name"] == "session"]
        if got and got[0]["sameSite"] == "strict":
            break
        time.sleep(0.3)
    assert [c["sameSite"] for c in got] == ["strict"]
    assert got[0]["value"] == "alice" and got[0]["httpOnly"] is True


def test_popup_keeps_opener_and_closes_itself(engine, start_host, parent, site):
    h = start_host(engine)
    h.create(1, site.url("/popup"))
    h.mark()
    parent.click(W // 2, TOP + (H - TOP) // 2)  # the button fills the page
    popup = h.wait_event("popup", opener=1)
    assert popup["id"] != 1
    assert site.report("opener") == "true"           # window.opener survived
    assert site.report("popup") == "signed-in:carol"  # and postMessage reached the page
    h.wait_event("closed", id=popup["id"])            # window.close() closed it


def test_dialog_round_trip(engine, start_host, site):
    h = start_host(engine)
    h.create(1, site.url("/confirm"), wait_loaded=False)
    dialog = h.wait_event("dialog", id=1)
    assert dialog["type"] == "confirm" and dialog["message"] == "Proceed with test?"
    h.send(cmd="dialog-reply", req=dialog["req"], ok=True, text="")
    assert site.report("confirm") == "true"


@pytest.mark.xfail(reason="needs cookies-dump and SameSite comparison (work item 2)", strict=True)
def test_private_end_discards_private_profile(engine, start_host, site):
    h = start_host(engine)
    secret = {**ALICE, "value": "incognito"}
    h.send(cmd="cookies-replace", jar="private", cookies=[secret])
    h.wait_event("cookies-ready", jar="private")
    h.create(5, site.url("/"), jar="private")
    assert site.wait_for(lambda e: e["path"] == "/")["user"] == "incognito"
    h.mark()
    h.send(cmd="private-end")
    h.wait_event("private-ended")
    h.wait_event("closed", id=5)  # private tabs close with the session
    # A new private session starts from nothing: no cookies, nothing left
    # in the old profile's jar.
    h.send(cmd="cookies-replace", jar="private", cookies=[])
    h.wait_event("cookies-ready", jar="private")
    assert h.cookies("private") == []
    site.reset()
    h.create(6, site.url("/"), jar="private")
    assert site.wait_for(lambda e: e["path"] == "/")["user"] is None


def red_width(parent):
    row = parent.row(TOP + 10, 0, W)
    return sum(1 for px in row if close_to(px, (255, 0, 0), 40))


def test_zoom_resizes_red_square(engine, start_host, parent, site):
    h = start_host(engine)
    h.create(1, site.url("/red"))
    deadline = time.time() + 10
    while red_width(parent) == 0 and time.time() < deadline:
        time.sleep(0.3)
    before = red_width(parent)
    assert 95 <= before <= 105, before
    # The shell sends Chromium-style levels: factor = 1.2 ** level.
    h.send(cmd="zoom", id=1, level=math.log(2) / math.log(1.2))
    deadline = time.time() + 10
    while red_width(parent) < 190 and time.time() < deadline:
        time.sleep(0.3)
    assert 190 <= red_width(parent) <= 210, red_width(parent)


def test_quit_exits_cleanly(engine, start_host, site):
    h = start_host(engine)
    h.create(1, site.url("/"))
    assert h.quit() == 0


# ------------------------------------------------------------- Blink only --
# Only the Chromium host checks subresources (WebKit checks page loads).

def test_resource_without_verdict_is_blocked(blink, start_host, site):
    """Fail closed: no answer for a subresource blocks it; the page still loads."""
    h = start_host(blink, {"HYBRID_CHECK_TIMEOUT_MS": "400"})
    h.on_classify = lambda ev: None  # Firefox never answers
    h.create(1, site.url(f"/third-party?via={site.other}"))
    nav = h.wait_event("check-timeout", kind="navigation")
    assert nav["allowed"] is True  # a stalled check never stops browsing
    res = h.wait_event("check-timeout", kind="resource")
    assert res["allowed"] is False and "pixel.svg" in res["url"]
    blocked = h.wait_event("blocked", id=1)
    assert blocked["reason"] == "no verdict"
    assert page_views(site, "/third-party")  # the page itself loaded
    time.sleep(1)
    assert not page_views(site, "/pixel.svg")  # the image never left the engine


def test_no_verdict_is_never_cached(blink, start_host, site):
    h = start_host(blink, {"HYBRID_CHECK_TIMEOUT_MS": "400"})
    silent = [True]
    h.on_classify = lambda ev: None if (silent[0] and ev["kind"] == "resource") else {}
    h.create(1, site.url(f"/third-party?via={site.other}"))
    h.wait_event("blocked", id=1)
    # Firefox answers again: the same request must be asked about anew.
    silent[0] = False
    h.mark()
    site.reset()
    h.send(cmd="reload", id=1)
    h.wait_event("classify", kind="resource")
    site.wait_for(lambda e: e["path"] == "/pixel.svg", what="image after reload")


def test_fail_open_env_restores_old_behaviour(blink, start_host, site):
    h = start_host(blink, {"HYBRID_CHECK_TIMEOUT_MS": "400", "HYBRID_FAIL_OPEN": "1"})
    h.on_classify = lambda ev: None
    h.create(1, site.url(f"/third-party?via={site.other}"))
    res = h.wait_event("check-timeout", kind="resource")
    assert res["allowed"] is True
    site.wait_for(lambda e: e["path"] == "/pixel.svg", what="image loaded despite no verdict")


def test_substituted_body_is_served(blink, start_host, site):
    h = start_host(blink)
    stand_in = "window.thirdPartyVersion = 'stand-in';"

    def answer(ev):
        if ev["url"].endswith("/thirdparty.js"):
            return {"cache": False, "redirectBody": b64(stand_in), "redirectMime": "application/javascript"}
        return {}

    h.on_classify = answer
    h.create(1, site.url(f"/stand-in-test?via={site.other}"))
    assert site.report("script") == "stand-in"
    assert not page_views(site, "/thirdparty.js")  # the real script was never fetched


def test_redirect_verdict_is_followed(blink, start_host, site):
    h = start_host(blink)

    def answer(ev):
        if ev["url"].endswith("/thirdparty.js"):
            return {"cache": False, "redirectURL": f"http://{site.other}/redirected.js"}
        return {}

    h.on_classify = answer
    h.create(1, site.url(f"/stand-in-test?via={site.other}"))
    assert site.report("script") == "redirected"
    assert not page_views(site, "/thirdparty.js")


def test_unsafe_page_is_reported(blink, start_host, site):
    h = start_host(blink)
    # Page-load verdicts are cached per host, so the flagged page lives on
    # another host than the first page.
    h.on_classify = lambda ev: {"reason": "phishing"} if ev["kind"] == "navigation" and "/red" in ev["url"] else {}
    h.create(1, site.url("/"))
    h.send(cmd="navigate", id=1, url=f"http://{site.other}/red")
    ev = h.wait_event("unsafe", id=1)
    assert ev["reason"] == "phishing"
    time.sleep(0.5)
    assert not page_views(site, "/red")
