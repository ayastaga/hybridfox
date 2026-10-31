#!/usr/bin/env bash
# Package this folder's finished build as a .deb for this machine's CPU.
#
#   ./setup.sh        # build first
#   ./make-deb.sh     # writes hybrid-browser_<version>_<arch>.deb here
#   sudo apt install ./hybrid-browser_*.deb
#
# The package installs to /opt/hybrid-browser, adds a `hybrid-browser`
# command and an app-menu entry, and enables Chromium's sandbox (which needs
# a setuid helper on distros that restrict user namespaces).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v dpkg-deb >/dev/null || die "dpkg-deb not found (this script is for Debian and Ubuntu)."
[[ -x "$ROOT/engine/build/out/chromium-host" && -x "$ROOT/third_party/firefox/firefox" ]] ||
  die "Nothing to package yet. Run ./setup.sh first."

ARCH="$(dpkg --print-architecture)"
FF_VERSION="$(sed -n 's/^Version=//p' "$ROOT/third_party/firefox/application.ini" | head -1)"
VERSION="0.1.$(date +%Y%m%d)+ff${FF_VERSION:-0}"
OUT="$ROOT/hybrid-browser_${VERSION}_${ARCH}.deb"

STAGE="$(mktemp -d "$ROOT/.deb-stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/pkg"
APP="$PKG/opt/hybrid-browser"

say "Staging files"
mkdir -p "$APP/engine/build" "$APP/third_party" "$PKG/DEBIAN" "$PKG/usr/bin" \
  "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/scalable/apps"
cp -a "$ROOT/hybrid" "$ROOT/shell" "$ROOT/README.md" "$APP/"
# Hard links where possible: the engine and Firefox are about 2 GB.
cp -al "$ROOT/engine/build/out" "$APP/engine/build/out" 2>/dev/null ||
  cp -a "$ROOT/engine/build/out" "$APP/engine/build/out"
cp -al "$ROOT/third_party/firefox" "$APP/third_party/firefox" 2>/dev/null ||
  cp -a "$ROOT/third_party/firefox" "$APP/third_party/firefox"
if [[ -x "$ROOT/engine/build/webkit/webkit-host" ]]; then
  mkdir -p "$APP/engine/build/webkit"
  cp -a "$ROOT/engine/build/webkit/webkit-host" "$APP/engine/build/webkit/"
fi
# Copies, not hard links, for files whose mode the package changes.
rm -f "$APP/engine/build/out/chrome-sandbox"
cp "$ROOT/engine/build/out/chrome-sandbox" "$APP/engine/build/out/chrome-sandbox"
chmod 4755 "$APP/engine/build/out/chrome-sandbox"
ln -s /opt/hybrid-browser/hybrid "$PKG/usr/bin/hybrid-browser"

cat > "$PKG/usr/share/applications/hybrid-browser.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Hybrid Browser
GenericName=Web Browser
Comment=Firefox and Chromium engines in one browser
Exec=hybrid-browser %u
Icon=hybrid-browser
Terminal=false
Categories=Network;WebBrowser;
MimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;
StartupWMClass=firefox
EOF

cat > "$PKG/usr/share/icons/hicolor/scalable/apps/hybrid-browser.svg" <<'EOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <path d="M32 4a28 28 0 0 1 0 56z" fill="#1f9ec4"/>
  <path d="M32 4a28 28 0 0 0 0 56z" fill="#e8590c"/>
  <circle cx="32" cy="32" r="11" fill="#fff"/>
</svg>
EOF

INSTALLED_KB="$(du -sk --apparent-size "$PKG" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<EOF
Package: hybrid-browser
Version: $VERSION
Architecture: $ARCH
Maintainer: Hybrid Browser prototype <noreply@example.invalid>
Installed-Size: $INSTALLED_KB
Section: web
Priority: optional
Depends: libgtk-3-0t64 | libgtk-3-0, libnss3, libx11-6, libx11-xcb1, libxcomposite1,
 libxdamage1, libxrandr2, libxkbcommon0, libgbm1, libdrm2, libpango-1.0-0,
 libatk-bridge2.0-0t64 | libatk-bridge2.0-0, libcups2t64 | libcups2,
 libasound2t64 | libasound2, libdbus-1-3, libdbus-glib-1-2 | libglib2.0-0
Recommends: libwebkit2gtk-4.1-0, libjson-glib-1.0-0
Description: Prototype browser with Firefox and Chromium engines in one window
 Every tab runs on Firefox's engine (Gecko) by default and can switch to
 Chromium's engine (Blink) and back, in the same window and tab strip, with
 one shared cookie jar so sign-ins carry over between engines.
 .
 This is a prototype. It bundles an official Firefox build and the Chromium
 Embedded Framework, and does not update itself.
EOF

cat > "$PKG/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
SANDBOX=/opt/hybrid-browser/engine/build/out/chrome-sandbox
if [ -f "$SANDBOX" ]; then
  chown root:root "$SANDBOX"
  chmod 4755 "$SANDBOX"
fi
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
exit 0
EOF
chmod 755 "$PKG/DEBIAN/postinst"

say "Building $(basename "$OUT") (compressing about 2 GB, this takes a few minutes)"
if dpkg-deb --help 2>&1 | grep -q zstd; then COMP=(-Zzstd -z9); else COMP=(-Zxz -z6); fi
dpkg-deb --root-owner-group "${COMP[@]}" --build "$PKG" "$OUT" >/dev/null

say "Done: $OUT ($(du -h "$OUT" | cut -f1))"
echo "Install with:  sudo apt install '$OUT'"
echo "Run with:      hybrid-browser   (or Hybrid Browser in the app menu)"
echo "Remove with:   sudo apt remove hybrid-browser"
