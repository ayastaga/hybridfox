# Third-party notices

HybridFox's own code (this repository) is licensed under the Mozilla Public
License 2.0; see [LICENSE](LICENSE). It contains no code copied from the
projects below. It downloads, links against or runs them as described here.

## Firefox

- **What:** an official Firefox release build from mozilla.org, pinned in
  `versions.json`, downloaded by `scripts/deps.py` into `third_party/firefox`
  and bundled in the `.deb` that `make-deb.sh` builds.
- **Licence:** Mozilla Public License 2.0 (with parts under other free
  licences; see `about:license` in the browser). The build is unmodified;
  its source is published by Mozilla at
  <https://archive.mozilla.org/pub/firefox/releases/> (`source/`).
- **What HybridFox adds to the install:** `defaults/pref/autoconfig.js`,
  `hybrid.cfg` (which loads the shell from this repository) and
  `distribution/policies.json` (which turns off the self-updater).

**Trademark.** "Firefox" and the Firefox logos are trademarks of the Mozilla
Foundation. Mozilla's trademark policy lets you redistribute *unmodified*
official builds under the Firefox name, but a build whose behaviour has been
changed (as here: autoconfig that loads a different browser layer, updates
turned off) may not be distributed as "Firefox" without permission. Running it
yourself, as `setup.sh` does, is fine. Before publishing the `.deb` or any
other package for others to download:

- build it from an **unbranded** Firefox (Mozilla publishes unbranded builds
  for add-on developers; they carry no Firefox name or logo), or build Firefox
  from source with branding off; and
- don't use the Firefox name or logo for the package (it is called HybridFox,
  and its icon is its own).

The `.deb` currently bundles the branded official build, so it is for your
own machines only.

## Chromium Embedded Framework (CEF) and Chromium

- **What:** a CEF "minimal" binary distribution from
  <https://cef-builds.spotifycdn.com/>, pinned in `versions.json`. The engine
  host (`engine/chromium_host.cc`) is compiled against its headers and
  `libcef_dll_wrapper`, and links `libcef.so`; the `.deb` bundles
  `libcef.so` and Chromium's resource files.
- **Licence:** CEF is under a BSD 3-clause licence
  (`third_party/cef/LICENSE.txt`). Chromium is under a BSD-style licence and
  includes many third-party components under their own licences, listed in
  `third_party/cef/CREDITS.html` where the distribution includes it. The
  `.deb` installs both files under `/opt/hybridfox/licenses/`.
- "Chromium" and "Chrome" are trademarks of Google LLC; HybridFox uses the
  names only to say which engine a tab runs on.

## WebKitGTK

- **What:** the WebKit engine host (`engine/webkit_host.c`) links the
  system's WebKitGTK (`libwebkit2gtk-4.1`), GTK 3, json-glib and libsoup 3.
  They are used from the operating system's packages; nothing is bundled.
- **Licence:** WebKit is under the GNU LGPL 2.1 (or later) and BSD licences;
  GTK, json-glib and libsoup are under the LGPL. Dynamic linking to the
  system copies keeps them replaceable, as the LGPL requires.

## Used only by the tests (not distributed)

| Component | Licence | Use |
| --- | --- | --- |
| uBlock Origin (AMO, pinned) | GPL-3.0 | installed as a temporary add-on in `tests/test_browser.py`, `scripts/bench.py`, `scripts/bridge_matrix.py` |
| ClearURLs (AMO, pinned) | LGPL-3.0 | `scripts/bridge_matrix.py` |
| Privacy Badger (AMO, pinned) | GPL-3.0 | `scripts/bridge_matrix.py` |
| python-xlib | LGPL-2.1 | stand-in Firefox window, XTEST clicks, pixel reads |
| pytest | MIT | test runner |
| Pillow | MIT-CMU (HPND) | screenshots of failed tests |
