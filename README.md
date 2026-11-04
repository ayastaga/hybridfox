# Hybrid browser prototype

One browser window, three engines. Every tab runs on Firefox's engine (Gecko)
by default, and any tab can switch to Chromium's engine (Blink) or WebKit and
back, inside the same window and tab strip, keeping your sign-ins.

This is a rough prototype: real engines, hacky glue. It runs an unmodified,
official Firefox build, Chromium 156 through CEF (the Chromium Embedded
Framework) and WebKit through WebKitGTK. Nothing is compiled from Firefox's,
Chromium's or WebKit's source.

## Requirements

The WebKit engine is optional; setup builds it when WebKitGTK's development
files are present (`sudo apt install libwebkit2gtk-4.1-dev libjson-glib-dev`
on Debian and Ubuntu) and tells you how to add them otherwise.


- x86_64 or ARM64 (aarch64) Linux, any desktop. On Wayland (GNOME, KDE) it runs through XWayland,
  which those desktops start automatically.
- About 4 GB of disk.
- Build tools: `curl xz bzip2 python3 cmake g++ make` and the X11 headers.

```bash
# Debian/Ubuntu
sudo apt install curl xz-utils bzip2 python3 cmake g++ make libx11-dev
# Fedora
sudo dnf install curl xz bzip2 python3 cmake gcc-c++ make libX11-devel
# Arch
sudo pacman -S --needed curl xz bzip2 python cmake gcc make libx11
```

## Build and run

```bash
chmod +x setup.sh   # make the setup script executable
./setup.sh          # downloads CEF + Firefox, builds the engine host (~10 min)
./hybrid            # starts the browser
```

### Installing as a .deb (Debian, Ubuntu)

After `./setup.sh`, you can package your build and install it like any app:

```bash
./make-deb.sh
sudo apt install ./hybrid-browser_*.deb
hybrid-browser
```

The package installs to `/opt/hybrid-browser`, adds a *Hybrid Browser* entry
to your app menu and turns on Chromium's sandbox during installation. It's
built for your machine's processor (x86_64 or ARM64) and is about 400 MB.
Remove it with `sudo apt remove hybrid-browser`.

`./hybrid --debug` also prints every message between the two engines to the
terminal. `./setup.sh --update` re-downloads the latest Firefox and CEF.

Your profile lives in `~/.local/share/hybrid-browser/`, separate from your
normal Firefox and Chrome profiles.

### Chromium's sandbox

Chromium's sandbox needs unprivileged user namespaces. Ubuntu 23.10 and later
restrict them, and the launcher then warns that Chromium tabs run without the
sandbox. To enable it, run once (paths are printed in the warning):

```bash
sudo chown root:root engine/build/out/chrome-sandbox
sudo chmod 4755 engine/build/out/chrome-sandbox
```

## Using it

- **Switch the current tab's engine:** click the engine button next to the
  address bar (**Gecko**, **Blink** or **WebKit**) and pick an engine, or press
  **Ctrl+Alt+E** to cycle Gecko → Blink → WebKit. The page reloads in the
  chosen engine.
- **Switch any tab:** right-click it and use *Tab Engine*.
- **Open a link with another engine:** right-click the link and choose *Open
  Link in New Blink Tab* or *Open Link in New WebKit Tab*.
- Blink tabs have a blue underline in the tab strip, WebKit tabs a purple one.

**You stay signed in across engines.** All engines share one cookie jar:
sign in on any engine and the others are signed in too, even tabs that are
already open, and signing out works the same way. Sign-in popups, and alert, confirm and prompt dialogs,
also work in Chromium tabs (the dialogs appear as Firefox's own prompts).

**Every window, including private windows.** Chromium tabs work in all
windows. In a private window they use a separate in-memory Chromium profile
that shares sign-ins only with that private session and is discarded,
together with Firefox's private data, when the last private window closes.
Nothing from a private Chromium tab is written to disk.

**Your Firefox extensions filter Chromium tabs' requests.** Extensions that
block or rewrite requests, such as uBlock Origin, Privacy Badger or ClearURLs,
decide every request a Chromium tab makes, with their own lists and your
settings: blocked requests are cancelled, and stand-ins an extension
substitutes (uBlock Origin's harmless replacement scripts) are served to the
page. This works because the blank Gecko page under each Chromium tab takes on
the site's identity and asks for each request from there, so extensions see
an ordinary Firefox request; nothing is downloaded twice.

**Firefox's protections apply to Chromium tabs.** Before a Chromium tab
loads a page, Firefox checks it with Safe Browsing; a phishing or malware
page opens in Gecko instead, showing Firefox's own warning. Third-party
requests from Chromium pages are checked against Firefox's Enhanced Tracking
Protection lists, following your protection level (Standard, Strict, Custom),
its private-window setting and your per-site exceptions. The engine button
shows how many requests were blocked ("Blink · 3 blocked").

**No Chromium traces on disk.** Chromium's working folder lives in a
temporary folder in memory (`$XDG_RUNTIME_DIR`) and is deleted when the
browser closes. Sign-ins come from Firefox's cookie jar, and pages you visit
in Chromium tabs are recorded in Firefox's history (except in private
windows). Set `HYBRID_CHROMIUM_PERSIST=1` to keep a Chromium profile instead.

**Per-site rules.** Right-click a tab and choose *Always Open example.com in
Chromium*; visits to that site then open with Chromium's engine. When a page
in a Gecko tab says it doesn't support your browser, a bar offers to open it
in Chromium, once or always. Rules can't be created from private windows,
since that would record a site you visited privately.

**Same size as Gecko tabs.** Chromium renders at exactly Firefox's scale on
HiDPI and scaled screens, and Ctrl+Plus, Ctrl+Minus and Ctrl+0 zoom
Chromium pages using Firefox's zoom steps (the engine button shows the
level, e.g. "Blink 120%").

Inside a Chromium tab, the address bar, bookmarks, search, back, forward and
reload (including F5 and Ctrl+R) all act on the Chromium page. Ctrl+T, Ctrl+W, Ctrl+L, Ctrl+N,
Ctrl+Tab, Ctrl+Shift+Tab, Ctrl+PageUp/PageDown, Ctrl+Shift+T and F6 work
while a Chromium page has the keyboard. Chromium tabs reopen as Chromium tabs
after a restart.

## How it works

```
 Firefox (official build)                      chromium-host (CEF 156)
 ┌───────────────────────────────────┐ stdin   ┌──────────────────────────┐
 │ hybrid.cfg   loads ──► hybrid-    │ ──────► │ one CEF browser per      │
 │ (autoconfig)          browser.js  │ JSON    │ Chromium tab             │
 │                                   │ ◄────── │ title/url/loading/keys   │
 │ tab strip, address bar, menus     │ stdout  │                          │
 └──────────────┬────────────────────┘         └────────────┬─────────────┘
                │ X11 window                                │ X11 child windows
                └──────────── Chromium views are placed over the tab's content area
```

- **Shell (`shell/`).** Firefox runs `hybrid.cfg` at startup through its
  built-in autoconfig mechanism. That loads `hybrid-browser.js` into each
  browser window. A Chromium tab is a normal Firefox tab whose Gecko page
  sits on `about:blank` while a Chromium view covers its content area. The
  script maps Firefox's tab strip, address bar, navigation buttons, menus,
  keyboard shortcuts and session restore onto Chromium.
- **Engine (`engine/chromium_host.cc`).** A C++ program on CEF. Firefox
  starts it as a helper process and sends it one JSON command per line
  (create, show, hide, move, navigate, back, forward, reload, focus, close).
  It answers with events (title, URL, loading state, favicons, popups and
  browser shortcuts pressed inside a Chromium page).
- **Embedding.** Each Chromium tab is an X11 child window inside Firefox's
  window, so it moves, resizes and stacks with Firefox natively. Each one
  lives in a small container window with the default X11 visual, because
  Firefox's window uses a different visual that Chromium's window can't
  inherit.
- **Shared cookie jar.** Firefox's cookie jar is the master copy. At startup
  Chromium's jar is replaced with it; after that every cookie Firefox adds,
  changes or deletes is pushed to Chromium immediately, and the engine
  compares Chromium's jar with Firefox's every second and sends back any
  differences (CEF has no change notification). Switching a tab from Chromium
  to Gecko syncs first, so the page reloads already signed in. Each side
  ignores changes it just received, so nothing bounces back and forth.
- **Popups.** A popup opened by a Chromium page stays a real Chromium popup,
  so it can still talk to the page that opened it (sign-in popups depend on
  this), and it appears as a new tab. When it closes itself, its tab closes.
- **Keyboard focus.** The window manager only sees Firefox's window, so the
  host watches mouse clicks. A click on a Chromium view gives it the
  keyboard, and a click anywhere else hands it back to Firefox through GTK's
  focus window.

## Testing the engine on its own

```bash
pip install python-xlib
python3 engine/test_host.py engine/build/out/chromium-host /tmp/shots
```

This opens a stand-in browser window, drives the engine through the same
protocol and saves screenshots: create, background tab, tab switch, resize,
live navigation, close and quit.

## Testing sign-in across engines

```bash
python3 tests/signin_site.py &      # a small sign-in site on http://127.0.0.1:8765
./hybrid http://127.0.0.1:8765/
```

Sign in, switch the tab's engine with Ctrl+Alt+E, and check you're still
signed in. The site also has a popup sign-in and a `confirm()` test.
`tests/chromium-only-test.html` shows features that work in Chromium but not
in Firefox.

## Troubleshooting

- **"No X11 display found":** on Wayland, XWayland must be enabled.
- **The Chromium view is offset from the content area:** set
  `HYBRID_OFFSET_X` / `HYBRID_OFFSET_Y` (device pixels) before `./hybrid`.
  Please report your desktop and scaling setting.
- **Blurry on a HiDPI or fractionally scaled screen:** both engines run on X11,
  so the desktop's XWayland scaling applies. On KDE, set *Legacy applications
  (X11): Apply scaling themselves*.
- **Chromium tabs feel slower than Gecko tabs:** open `chrome://gpu` in a
  Chromium tab. In a VM without GPU passthrough, Chromium falls back to slow
  software rendering. Give the VM a GPU (in UTM: Display → the card marked
  "GPU Supported"), or try forcing plain software mode, which avoids GPU
  process crashes:
  `HYBRID_CHROMIUM_FLAGS="--disable-gpu" ./hybrid`. Any Chromium switches can
  be passed this way.
- **Nothing happens when switching engines:** run `./hybrid --debug` and look
  for lines starting with `[hybrid]`.

## Known limits

- Only cookies are shared. Site storage (localStorage, sessionStorage,
  IndexedDB) is still separate per engine, so a site that keeps its login
  token there may sign you in again silently or ask once.
- Container tabs and partitioned third-party cookies are not shared with
  Chromium.
- Extensions that work inside pages (password-manager autofill, Dark Reader,
  userscripts, uBlock Origin's element hiding) don't reach engine tabs yet;
  only request filtering does.
- An extension's own toolbar popup (for example uBlock Origin's per-site
  switch and its counter) sees a blank page for engine tabs, because Firefox
  doesn't let the placeholder take on the page's address.
- WebKit tabs get Safe Browsing on page loads, but not yet tracking
  protection or extension filtering of the requests inside pages.
- WebKit matches Firefox's scale with zoom on fractional display scales, so
  images can look slightly soft there.
- Sites stored in Chromium (localStorage, IndexedDB) are forgotten when the
  browser closes; sign-ins survive because cookies come from Firefox.
- Touchpad scrolling has no momentum ("kinetic" scrolling), in Gecko and
  Chromium tabs alike, because both engines run through X11.
- The Chromium scale is fixed when the browser starts; after changing your
  display scaling, restart the browser.
- Dragging a Chromium tab to another window reloads it there.
- Passkeys and security keys don't work in Chromium tabs yet.
- Google blocks signing in from embedded Chromium. Sign in to Google in a
  Gecko tab instead; the shared cookies should then carry over.
- Switching engines reloads the page. Unsaved form input doesn't carry over.
- The address bar's suggestion list hides the Chromium view while it's open.
- Firefox dialogs drawn inside the window (not separate popups) can end up
  behind a Chromium view.
- No Chromium extensions, no Widevine DRM, no printing or find-in-page in
  Chromium tabs yet. Downloads and file pickers use Chromium's defaults.
- Firefox self-updates are disabled for this install. Use
  `./setup.sh --update` instead.
- ARM64 builds use the same code but haven't been tested yet.

## Layout

```
hybrid              launcher
setup.sh            downloads, build, install
make-deb.sh         package a finished build as a .deb
shell/
  hybrid.cfg        Firefox autoconfig bootstrap
  autoconfig.js     tells Firefox to run hybrid.cfg
  policies.json     disables Firefox's self-updater for this install
  hybrid-browser.js the per-window layer (Chromium tabs, UI, routing)
  HybridHost.sys.mjs the shared engine connection and cookie mirroring
  HybridPrivacy.sys.mjs Firefox's Safe Browsing and tracking protection verdicts
engine/
  chromium_host.cc  the Chromium engine host
  webkit_host.c     the WebKit engine host (built when WebKitGTK is installed)
  CMakeLists.txt
  test_host.py      standalone engine test
tests/
  signin_site.py    local sign-in site for testing shared logins
  chromium-only-test.html  page that works in Chromium but not Firefox
third_party/        CEF and Firefox (created by setup.sh)
```

## Next steps

1. Share the whole network layer (cache, site storage, HTTP auth), not just
   cookies, by giving Chromium a network layer backed by Firefox's.
2. Render Chromium off-screen into Firefox's compositor, which allows native
   Wayland, correct stacking of in-window dialogs and no XWayland.
3. Chromium tabs in every window, find-in-page, printing, downloads through
   Firefox's download manager.
4. Load the engine into Firefox's own process.
