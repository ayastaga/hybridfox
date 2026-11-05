#!/usr/bin/env python3
"""Pinned downloads for HybridFox: Firefox and CEF (Chromium Embedded Framework).

  scripts/deps.py fetch               download what versions.json pins and isn't here yet
  scripts/deps.py update              move the pins to the latest stable releases, then fetch
  scripts/deps.py fetch-test-addons   the extensions the browser tests install
                                      (uBlock Origin), into third_party/test-addons

versions.json records, per CPU, the exact Firefox archive with its SHA-256
(from Mozilla's SHA256SUMS) and the exact CEF archive with its SHA-1 (from
CEF's index.json). Every download is checked against it before unpacking, so
a build is reproducible and a tampered or truncated file is refused.

Each unpacked folder gets a .pinned marker naming what was unpacked; fetch
only downloads when the marker doesn't match the pin. A CEF change also wipes
engine/build, because the engine host must be rebuilt against the new headers.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TP = os.path.join(ROOT, "third_party")
VERSIONS = os.path.join(ROOT, "versions.json")
# Overridable so tests can serve archives from a local folder.
CEF_CDN = os.environ.get("HYBRID_CEF_CDN", "https://cef-builds.spotifycdn.com/")
FF_RELEASES = "https://archive.mozilla.org/pub/firefox/releases/"
# Our CPU names -> each project's platform names.
ARCHES = {"x86_64": ("linux64", "linux-x86_64"), "aarch64": ("linuxarm64", "linux-aarch64")}


def say(msg):
    print(f"\033[1;36m==>\033[0m {msg}", flush=True)


def die(msg):
    print(f"\033[1;31merror:\033[0m {msg}", file=sys.stderr)
    sys.exit(1)


def arch():
    m = platform.machine()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "aarch64"
    die(f"Unsupported CPU {m}. Supported: x86_64 and aarch64 (ARM64).")


def load():
    with open(VERSIONS) as f:
        return json.load(f)


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def download(url, dest):
    # curl resumes and retries; the archives are hundreds of MB and the CDNs
    # occasionally reset long transfers.
    subprocess.run(
        ["curl", "-fL", "--retry", "8", "--retry-all-errors", "-C", "-",
         "--progress-bar", "-o", dest, url],
        check=True,
    )


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_marker(folder):
    try:
        with open(os.path.join(folder, ".pinned")) as f:
            return f.read().strip()
    except OSError:
        return None


def fetch_one(label, url, algo, expected, folder, marker, unpack):
    """Download |url| into |folder| unless its marker already says |marker|."""
    if read_marker(folder) == marker:
        say(f"{label}: up to date ({marker.split()[0]})")
        return False
    os.makedirs(TP, exist_ok=True)
    archive = os.path.join(TP, ".download-" + os.path.basename(urllib.parse.unquote(url)))
    if os.path.exists(archive) and digest(archive, algo) == expected:
        say(f"{label}: using the already downloaded archive")
    else:
        say(f"{label}: downloading {url}")
        download(url, archive)
        got = digest(archive, algo)
        if got != expected:
            os.remove(archive)
            die(f"{label}: checksum mismatch ({algo} {got}, versions.json pins {expected}). "
                "The download was refused.")
    say(f"{label}: {algo} verified, unpacking")
    staging = tempfile.mkdtemp(dir=TP, prefix=".unpack-")
    try:
        content = unpack(archive, staging)
        shutil.rmtree(folder, ignore_errors=True)
        os.rename(content, folder)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    with open(os.path.join(folder, ".pinned"), "w") as f:
        f.write(marker + "\n")
    os.remove(archive)
    return True


def untar_single_top(archive, staging):
    # Both archives hold one top-level folder (firefox/, cef_binary_.../),
    # which becomes the destination folder. tar handles both compressions and
    # keeps symlinks and modes as packaged.
    subprocess.run(["tar", "-xf", archive, "-C", staging], check=True)
    tops = os.listdir(staging)
    if len(tops) != 1:
        die(f"unexpected archive layout in {archive}: {tops}")
    return os.path.join(staging, tops[0])


def fetch():
    v = load()
    a = arch()
    ff = v["firefox"]["files"][a]
    fetch_one(
        f"Firefox {v['firefox']['version']}", ff["url"], "sha256", ff["sha256"],
        os.path.join(TP, "firefox"), f"{v['firefox']['version']} {ff['sha256']}", untar_single_top,
    )
    cef = v["cef"]["files"][a]
    changed = fetch_one(
        f"CEF {v['cef']['version']}", CEF_CDN + urllib.parse.quote(cef["name"]), "sha1", cef["sha1"],
        os.path.join(TP, "cef"), f"{v['cef']['version']} {cef['sha1']}", untar_single_top,
    )
    if changed:
        say("CEF changed: removing engine/build so the engine host is rebuilt")
        shutil.rmtree(os.path.join(ROOT, "engine", "build"), ignore_errors=True)


def fetch_test_addons():
    """Extensions the tests install, pinned like everything else: an add-on
    that changes under the tests would change what they measure."""
    folder = os.path.join(TP, "test-addons")
    os.makedirs(folder, exist_ok=True)
    for name, pin in load().get("test_addons", {}).items():
        dest = os.path.join(folder, name + ".xpi")
        if os.path.exists(dest) and digest(dest, "sha256") == pin["sha256"]:
            say(f"{name} {pin['version']}: up to date")
            continue
        say(f"{name} {pin['version']}: downloading {pin['url']}")
        tmp = dest + ".part"
        download(pin["url"], tmp)
        got = digest(tmp, "sha256")
        if got != pin["sha256"]:
            os.remove(tmp)
            die(f"{name}: checksum mismatch (sha256 {got}, versions.json pins {pin['sha256']})")
        os.rename(tmp, dest)


def version_key(cef_version):
    # "156.0.14+g4fa4ab5+chromium-156.0.8078.48" -> (156, 0, 14). index.json
    # isn't sorted newest first, so compare versions instead of trusting order.
    return tuple(int(x) for x in cef_version.split("+")[0].split("."))


def update():
    v = load()
    say("Finding the latest stable CEF build for every CPU")
    index = json.loads(get(CEF_CDN + "index.json"))
    per_arch = {}
    for a, (cef_plat, _) in ARCHES.items():
        for ver in index[cef_plat]["versions"]:
            if ver["channel"] != "stable":
                continue
            minimal = next((f for f in ver["files"] if f["type"] == "minimal"), None)
            if minimal:
                per_arch.setdefault(a, {})[ver["cef_version"]] = minimal
    common = set.intersection(*(set(d) for d in per_arch.values())) if len(per_arch) == len(ARCHES) else set()
    if not common:
        die("no stable CEF build exists for every CPU")
    cef_version = max(common, key=version_key)
    v["cef"] = {
        "version": cef_version,
        "files": {a: {"name": per_arch[a][cef_version]["name"], "sha1": per_arch[a][cef_version]["sha1"]}
                  for a in ARCHES},
    }

    say("Finding the latest Firefox release")
    ff_version = json.loads(get("https://product-details.mozilla.org/1.0/firefox_versions.json"))[
        "LATEST_FIREFOX_VERSION"]
    sums = get(f"{FF_RELEASES}{ff_version}/SHA256SUMS").decode()
    files = {}
    for a, (_, ff_plat) in ARCHES.items():
        found = None
        for ext in ("tar.xz", "tar.bz2"):
            rel = f"{ff_plat}/en-US/firefox-{ff_version}.{ext}"
            found = next((line.split()[0] for line in sums.splitlines() if line.endswith("  " + rel)), None)
            if found:
                files[a] = {"url": f"{FF_RELEASES}{ff_version}/{rel}", "sha256": found}
                break
        if not found:
            die(f"Firefox {ff_version} has no en-US build for {ff_plat}")
    v["firefox"] = {"version": ff_version, "files": files}

    with open(VERSIONS, "w") as f:
        json.dump(v, f, indent=2)
        f.write("\n")
    say(f"Pinned Firefox {ff_version} and CEF {cef_version} in versions.json")
    fetch()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "fetch":
        fetch()
    elif cmd == "update":
        update()
    elif cmd == "fetch-test-addons":
        fetch_test_addons()
    else:
        print(__doc__.strip())
        sys.exit(2)
