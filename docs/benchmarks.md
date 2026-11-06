# Benchmarks

Measured with [`scripts/bench.py`](../scripts/bench.py); the raw samples
are in [`bench-results.json`](bench-results.json). Every number is a median,
with the 25th–75th percentile range in brackets, over the number of runs
shown (n). Nothing here is estimated or extrapolated.

```bash
xvfb-run -a -s "-screen 0 1280x1024x24" python3 scripts/bench.py --runs 7
```

## The machine

| | |
| --- | --- |
| Host | MacBook Air, Apple M4 (10 cores), 16 GB |
| Guest (where everything ran) | UTM virtual machine: Ubuntu 26.04, aarch64, 4 virtual CPUs, 7.2 GB RAM |
| Display | Xvfb (virtual X server), no GPU: Chromium runs with `--disable-gpu`, everything renders in software |
| Builds | Firefox 159.0, CEF 156.0.14 (Chromium 156.0.8078.48), WebKitGTK 2.52.6 (Ubuntu's), uBlock Origin 1.75.4 |
| Sandbox | Chromium's sandbox off (`HYBRID_NO_SANDBOX=1`), as in the tests |
| Date | 2026-11-05 |

A real desktop with a GPU will be faster at rendering; the relative costs
(what a switch, a cookie hop or a bridge check adds) are the useful part.

## Switching a loaded tab between engines

From the switch command until the new engine has fetched the page from the
local test site and finished loading it (a small page). n = 7 per pair.

| From → to | Median | p25–p75 |
| --- | ---: | ---: |
| Gecko → Blink | 150 ms | 92–340 ms |
| Gecko → WebKit | 305 ms | 218–319 ms |
| Blink → Gecko | 84 ms | 71–275 ms |
| Blink → WebKit | 337 ms | 241–473 ms |
| WebKit → Gecko | 46 ms | 44–61 ms |
| WebKit → Blink | 69 ms | 66–218 ms |

- The engine processes were already running; the first switch to an engine
  in a session also starts its process (not measured here).
- Leaving Blink or WebKit first pulls the engine's latest cookies into
  Firefox, so the page reloads signed in. That pull waits for the engine's
  cookie read, which is why some pairs are bimodal (e.g. WebKit → Blink:
  66 ms typically, ~220 ms when it waits).
- Switching into WebKit is the slowest of the pairs here; each WebKit tab
  gets a new web view. (Where that time goes wasn't broken down.)

## Cookie propagation

A page in a Blink tab sets a cookie; a page in a WebKit tab, open the whole
time, polls `document.cookie` every 10 ms. Times are taken on one clock:
the test site's log (cookie issued, cookie seen by WebKit's page) and an
observer inside Firefox (cookie in Firefox's jar). n = 7.

| Hop | Median | p25–p75 |
| --- | ---: | ---: |
| Blink → Firefox's jar | 895 ms | 614–923 ms |
| Firefox's jar → WebKit page | 11 ms | 6–12 ms |
| Total | 907 ms | 624–930 ms |

Engine → Firefox is the slow hop. CEF has no cookie-change notification,
so the Blink host compares its jar with Firefox's once a second; that poll
interval is the main reason for the 0.6–0.9 s measured here (why the median
sits nearer 0.9 s than the 0.5 s an evenly timed poll would give wasn't
investigated). Firefox → engine is pushed immediately. Switching engines
doesn't wait for the poll: it asks the engine for its cookies first.

## Request checks and the extension bridge

A page with 50 third-party images (served locally, so the network doesn't
blur the result), in a Blink tab, in three setups:

- **checks off:** `HYBRID_NO_PROTECTION=1`, the engine asks Firefox nothing;
- **Firefox checks:** Firefox's own checks, no extensions (verdicts depend
  only on the sites involved and are cached, so after the first load each
  image is answered from the engine's cache). Note: Marionette, which drives
  the browser here, turns Safe Browsing off, so the page load itself wasn't
  looked up; the images' tracking-protection lookups were;
- **uBlock Origin:** every request is asked through the extension bridge
  (extension verdicts are per request and never cached).

"Load" is from the navigate command until the engine reports the page
loaded; "page → last image" is server-side, from the page request to the
50th image request. n = 15.

| Setup | Load | Page → last image |
| --- | ---: | ---: |
| checks off | 119 ms (100–190) | 76 ms (65–110) |
| Firefox checks | 107 ms (95–121) | 65 ms (62–85) |
| uBlock Origin | 146 ms (141–177) | 110 ms (103–123) |

- Firefox's own checks cost nothing measurable once cached (the medians are
  within the noise of "checks off").
- Through the bridge with uBlock Origin, 50 requests took about 27–34 ms
  longer: roughly **0.6–0.7 ms per request**, with the requests checked in
  parallel.

### Real sites

The same three setups on real sites, over the internet from the VM. n = 7
(Wikipedia "checks off": 6 of 7 loads finished within 60 s). The setups ran
one after another, minutes apart, so network conditions differ between them:
treat differences under a few hundred milliseconds as noise.

| Page | checks off | Firefox checks | uBlock Origin |
| --- | ---: | ---: | ---: |
| en.wikipedia.org/wiki/Web_browser | 290 ms (236–626) | 381 ms (283–578) | 417 ms (294–639) |
| theguardian.com/international | 668 ms (633–766) | 830 ms (688–951) | 1144 ms (628–3207) |
| bbc.com/news | 1502 ms (981–2028) | 1610 ms (1539–1791) | 1334 ms (1018–1876) |

On a heavy news page uBlock Origin can make the page *faster* (BBC: ads and
trackers that would load are blocked) or slower (The Guardian, with a wide
spread); the bridge's per-request cost above is small next to network time.

## Memory

Proportional set size (PSS: shared pages split between the processes that
share them) of each process tree, with one small page open in a Blink tab
and one in a WebKit tab. n = 3 (fresh browser each time; the three runs
were within 6 MB of each other).

| Process tree | PSS |
| --- | ---: |
| Firefox (all its processes) | 617 MB |
| Blink engine (chromium-host and its Chromium processes) | 449 MB |
| WebKit engine (webkit-host and its web processes) | 437 MB |

Each engine adds about 440–450 MB, once, when its first tab opens; an
engine is only started when a tab needs it.

## Not measured

- Startup time of each engine process (first switch in a session).
- Anything with a GPU, HiDPI scaling or Chromium's sandbox on.
- x86_64: CI runs the tests there, not the benchmarks.
