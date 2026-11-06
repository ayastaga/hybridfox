# The extension bridge: what it covers, measured

Firefox extensions can't see Chromium's network traffic. HybridFox's
*extension bridge* asks them anyway: for every request a Blink tab's page
makes, the blank Gecko page under that tab (the *placeholder*, which carries
the site's identity) opens an equivalent Firefox request that is never sent
(`LOAD_ONLY_FROM_CACHE`). Extensions see it through `webRequest` like any
other request and decide: cancel it, redirect it to a stand-in they provide,
or redirect it to another URL. The answer goes back to the engine, which
blocks, serves the stand-in, or follows the redirect.
[`hybrid-browser.js`](../shell/hybrid-browser.js) (`probe()`) and
[`chromium_host.cc`](../engine/chromium_host.cc) (`OnBeforeResourceLoad`)
hold the details.

This page says what that does and doesn't cover, from measurements, not
expectations. WebKit tabs have no bridge; everything below is about Blink.

## How it was measured

[`scripts/bridge_matrix.py`](../scripts/bridge_matrix.py) runs every
combination of setup (no extension, uBlock Origin, ClearURLs, Privacy
Badger; pinned versions in [`versions.json`](../versions.json)) and engine
(Gecko, Blink) in a fresh browser, on fixture pages from
[`tests/signin_site.py`](../tests/signin_site.py). The site logs every
request it receives (path, query, headers) and pages report what they saw,
so "Blink matches Gecko" means the same thing reached the server and the
page. Raw results: [`bridge-matrix.json`](bridge-matrix.json).

Run on 2026-11-05 on the machine described in [benchmarks.md](benchmarks.md):
uBlock Origin 1.75.4, ClearURLs 1.27.3, Privacy Badger 2026.9.15, each with
its default settings, Firefox 159.0, CEF 156.0.14.

The fixtures are served as `fixtures.test` and `third.test` (mapped to this
machine in both engines), because ClearURLs by default leaves local
addresses such as `127.0.0.1` alone. The ad fixtures load Google's real ad
script and a real DoubleClick image, so they need network access.

## Coverage matrix

What each kind of extension action does in a Blink tab:

| Extension action | Bridged? | Evidence |
| --- | --- | --- |
| Block a request | **Yes** | uBlock Origin blocks the DoubleClick image in Blink as in Gecko |
| Redirect to a stand-in it ships (a "surrogate") | **Yes** | uBlock Origin's stand-in for `adsbygoogle.js` runs in Blink, identical to Gecko's |
| Redirect a subresource to another URL | **Yes** | ClearURLs strips `utm_*` from a third-party image in Blink as in Gecko |
| Redirect the page itself (main frame) | **No** | ClearURLs strips `utm_*` from the page address in Gecko, not in Blink |
| Change request headers | **No** | Privacy Badger adds `DNT: 1` and `Sec-GPC: 1` in Gecko; Blink sends neither |
| Change response headers | **No** (not measured) | the bridge's request is never sent, so there is no response to change |
| WebSocket connections | **No** | the Blink host asked Firefox about 0 WebSocket connections |
| Decisions that depend on the tab's page load | **No** | Privacy Badger blocks DoubleClick in Gecko, not in Blink |

## Fidelity: the same fixtures in Gecko and in Blink

`blocked`/`loaded` is what the page saw for the DoubleClick image;
"stand-in" is whether the ad script that ran was uBlock Origin's (Google's
real script leaves `adsbygoogle.pageState` behind, the stand-in doesn't);
the query columns are what the server received.

| Setup | Engine | Ad script | DoubleClick | Image query | Page query | DNT / GPC | WebSocket checked |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | Gecko | real | loaded | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | – / – | – |
| none | Blink | real | loaded | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | – / – | 0 |
| uBlock Origin | Gecko | **stand-in** | **blocked** | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | – / – | – |
| uBlock Origin | Blink | **stand-in** | **blocked** | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | – / – | 0 |
| ClearURLs | Gecko | real | loaded | **`id=7`** | **`id=7`** | – / – | – |
| ClearURLs | Blink | real | loaded | **`id=7`** | `utm_source=…&id=7` | – / – | 0 |
| Privacy Badger | Gecko | real | **blocked** | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | **1 / 1** | – |
| Privacy Badger | Blink | real | loaded | `utm_source=…&utm_medium=…&id=7` | `utm_source=…&id=7` | – / – | 0 |

(uBlock Origin's default lists don't strip `utm_*`; that is expected, not a
gap.) In Blink, the engine also recorded *why* each request was changed:
`adsbygoogle.js` "replaced by an extension", DoubleClick "extension".

So, of the three extensions: **uBlock Origin** behaves the same in Blink as
in Gecko on these fixtures; **ClearURLs** works for a page's requests but
not for the page's own address; **Privacy Badger** does nothing in Blink.

## Latency

From [benchmarks.md](benchmarks.md), a page with 50 third-party images in a
Blink tab: with uBlock Origin (every request asked through the bridge, since
extension answers are never cached) the 50 requests took about 27–34 ms
longer than with no checks at all, roughly **0.6–0.7 ms per request**, with
requests checked in parallel. Firefox's own checks (no extension) cost
nothing measurable once their per-site answers are cached. On real news
sites the difference disappears in network time.

## Limits, and why

- **Page loads aren't asked.** The bridge asks about the requests a page
  makes, not the page load itself: an engine tab's address bar, links and
  redirects are checked by Firefox's Safe Browsing only. Extensions that
  act on page loads (ClearURLs cleaning addresses, blocking whole sites,
  HTTPS upgrades) don't apply. Bridging them would mean holding every
  navigation for an extension round trip and then translating "redirect"
  and "cancel" into engine navigations; possible, not done.
- **Extensions that follow the tab's page load.** Privacy Badger only acts
  on a tab after seeing that tab's main-frame request: its
  `onBeforeRequest` (`js/webrequest.js` in 2026.9.15) records the tab's
  first party on `main_frame` and returns early for any other request when
  it has none. The placeholder gets its identity without a page load
  (`createAboutBlankDocumentViewer`), so Privacy Badger never learns the
  tab's site and ignores its requests. A real (but cached, never-sent) page
  load in the placeholder might fix this; untested.
- **Headers.** The bridge's request is a decision probe that is never sent,
  and the engine's real request never passes through Firefox. Changes an
  extension makes in `onBeforeSendHeaders` (Privacy Badger's DNT and Global
  Privacy Control signals, header-stripping extensions) and
  `onHeadersReceived` (CSP injection, cookie stripping) can't reach the
  engine. Copying header changes back would need the probe to run that far,
  then a way to apply them to the engine's request (CEF allows editing
  request headers in `OnBeforeResourceLoad`).
- **WebSockets.** The Blink host only checks `http:` and `https:` requests
  (`IsWeb()` in `chromium_host.cc`), so `ws:` and `wss:` connections are
  neither checked nor blocked; measured: 0 checks for a WebSocket the page
  opened. (Whether CEF would even offer them to that hook wasn't tested.)
- **Scripts inside pages.** Content scripts run only in Gecko documents, so
  anything an extension does *inside* the page (uBlock Origin's cosmetic
  filtering and scriptlets, password managers, Dark Reader, user scripts)
  doesn't happen in engine tabs.
- **Toolbar popups see a blank page.** An extension's popup asks the tab
  for its address and finds the placeholder's `about:blank`: Firefox refuses
  to give it the site's address (`history.replaceState` to it fails with a
  SecurityError, which HybridFox logs as "address mirroring unavailable").
  So uBlock Origin's per-site switch and counters don't describe engine
  tabs.
- **WebKit tabs** have no bridge at all: only page loads are checked, by
  Safe Browsing.
- **Not measured:** extensions built on `declarativeNetRequest` (Manifest
  V3 blocking) rather than `webRequest`; extensions' allowlists and per-site
  switches; private windows.
