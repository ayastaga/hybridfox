#!/usr/bin/env bash
# Hybrid browser prototype: one-time setup.
#
#   ./setup.sh            download CEF + Firefox, build the Chromium engine host
#   ./setup.sh --update   re-download Firefox and CEF at their latest versions
#
# Needs: bash, curl, tar, xz, bzip2, python3, cmake (3.21+), g++ (C++20), make,
# and X11 development headers. Disk: about 4 GB.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TP="$ROOT/third_party"
UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- checks --
missing=()
for tool in curl tar xz bzip2 python3 cmake g++ make; do
  command -v "$tool" >/dev/null || missing+=("$tool")
done
if [[ ! -f /usr/include/X11/Xlib.h ]]; then missing+=("X11 headers"); fi
if (( ${#missing[@]} )); then
  echo "Missing: ${missing[*]}"
  echo
  echo "Install them with one of:"
  echo "  Debian/Ubuntu: sudo apt install curl xz-utils bzip2 python3 cmake g++ make libx11-dev"
  echo "  Fedora:        sudo dnf install curl xz bzip2 python3 cmake gcc-c++ make libX11-devel"
  echo "  Arch:          sudo pacman -S --needed curl xz bzip2 python cmake gcc make libx11"
  echo "  openSUSE:      sudo zypper install curl xz bzip2 python3 cmake gcc-c++ make libX11-devel"
  exit 1
fi
case "$(uname -m)" in
  x86_64)        CEF_PLATFORM=linux64;    FF_OS=linux64;         CEF_ARCH=x86_64 ;;
  aarch64|arm64) CEF_PLATFORM=linuxarm64; FF_OS=linux64-aarch64; CEF_ARCH=arm64 ;;
  *) die "Unsupported CPU $(uname -m). Supported: x86_64 and aarch64 (ARM64)." ;;
esac
mkdir -p "$TP"

# ------------------------------------------------------------------ CEF --
if [[ ! -d "$TP/cef" || $UPDATE == 1 ]]; then
  say "Finding the latest stable CEF (Chromium Embedded Framework) build"
  read -r CEF_NAME < <(curl -fsSL https://cef-builds.spotifycdn.com/index.json | python3 -c '
import json, sys
for v in json.load(sys.stdin)[sys.argv[1]]["versions"]:
    if v["channel"] == "stable":
        for f in v["files"]:
            if f["type"] == "minimal":
                print(f["name"]); sys.exit()
' "$CEF_PLATFORM")
  [[ -n "$CEF_NAME" ]] || die "Could not find a CEF build."
  say "Downloading $CEF_NAME (about 330 MB)"
  URL="https://cef-builds.spotifycdn.com/$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$CEF_NAME")"
  curl -fL --progress-bar -o "$TP/cef.tar.bz2" "$URL"
  rm -rf "$TP/cef" "$TP"/cef_binary_*
  tar -xjf "$TP/cef.tar.bz2" -C "$TP"
  mv "$TP"/cef_binary_* "$TP/cef"
  rm -f "$TP/cef.tar.bz2"
  rm -rf "$ROOT/engine/build"
fi

# ---------------------------------------------------- runtime libraries --
# libcef.so needs common desktop libraries that minimal installs may lack.
missing_libs="$(ldd "$TP/cef/Release/libcef.so" 2>/dev/null | awk '/not found/ {print $1}' | sort -u)"
if [[ -n "$missing_libs" ]]; then
  echo "Chromium needs some system libraries that aren't installed:"
  echo "$missing_libs" | sed 's/^/  /'
  echo
  echo "Install them with one of:"
  echo "  Ubuntu 24.04+ / Debian 13+:"
  echo "    sudo apt install libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \\"
  echo "      libdbus-1-3 libcups2t64 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libxkbcommon0 \\"
  echo "      libcairo2 libpango-1.0-0 libasound2t64 libgbm1 libdrm2 libgtk-3-0t64"
  echo "  Ubuntu 22.04 / Debian 12: the same, without the 't64' endings"
  echo "  Fedora: sudo dnf install nss nspr atk at-spi2-atk at-spi2-core dbus-libs cups-libs \\"
  echo "    libXcomposite libXdamage libXfixes libXrandr libxkbcommon cairo pango alsa-lib mesa-libgbm libdrm gtk3"
  echo "  Arch: sudo pacman -S --needed nss nspr at-spi2-core dbus libcups libxcomposite libxdamage \\"
  echo "    libxfixes libxrandr libxkbcommon cairo pango alsa-lib mesa libdrm gtk3"
  echo
  echo "Then run ./setup.sh again."
  exit 1
fi

# --------------------------------------------------------------- engine --
say "Building the Chromium engine host"
mkdir -p "$ROOT/engine/build"
# CEF's CMake files only detect ARM on macOS and Windows, so set it here.
cmake -S "$ROOT/engine" -B "$ROOT/engine/build" -DCMAKE_BUILD_TYPE=Release \
  -DPROJECT_ARCH="$CEF_ARCH" >/dev/null
cmake --build "$ROOT/engine/build" --target chromium-host -j"$(nproc)"

# ---------------------------------------------------------- WebKit engine --
# Optional: needs WebKitGTK (GTK 3 flavour), json-glib and libsoup 3.
WK_PKGS="webkit2gtk-4.1 gtk+-3.0 gtk+-x11-3.0 json-glib-1.0 libsoup-3.0 x11"
if pkg-config --exists $WK_PKGS 2>/dev/null; then
  say "Building the WebKit engine host"
  mkdir -p "$ROOT/engine/build/webkit"
  gcc -O2 -Wall -Wno-deprecated-declarations -o "$ROOT/engine/build/webkit/webkit-host" \
    "$ROOT/engine/webkit_host.c" $(pkg-config --cflags --libs $WK_PKGS) -lm
else
  warn "Skipping the WebKit engine: WebKitGTK development files aren't installed."
  echo "  To add it:  Debian/Ubuntu: sudo apt install libwebkit2gtk-4.1-dev libjson-glib-dev pkg-config"
  echo "              Fedora:        sudo dnf install webkit2gtk4.1-devel json-glib-devel pkgconf"
  echo "              Arch:          sudo pacman -S --needed webkit2gtk-4.1 json-glib pkgconf"
  echo "  then run ./setup.sh again."
fi

# -------------------------------------------------------------- Firefox --
if [[ ! -x "$TP/firefox/firefox" || $UPDATE == 1 ]]; then
  say "Downloading the latest Firefox release (official build from mozilla.org)"
  curl -fL --progress-bar -o "$TP/firefox.tar" \
    "https://download.mozilla.org/?product=firefox-latest-ssl&os=$FF_OS&lang=en-US"
  rm -rf "$TP/firefox"
  tar -xf "$TP/firefox.tar" -C "$TP"
  rm -f "$TP/firefox.tar"
fi

say "Installing the hybrid layer into Firefox"
install -Dm644 "$ROOT/shell/autoconfig.js" "$TP/firefox/defaults/pref/autoconfig.js"
install -Dm644 "$ROOT/shell/hybrid.cfg" "$TP/firefox/hybrid.cfg"
# Firefox's updater would replace the files above, so updates come from
# `./setup.sh --update` instead.
install -Dm644 "$ROOT/shell/policies.json" "$TP/firefox/distribution/policies.json"

chmod +x "$ROOT/hybrid"
say "Done. Start the browser with: $ROOT/hybrid"
