"""Static checks for mistakes that were made once and are easy to repeat."""
import os
import re
import subprocess

import pytest

from hostlib import HOSTS, ROOT


def files(*exts):
    out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True)
    names = out.stdout.split() if out.returncode == 0 else [
        os.path.relpath(os.path.join(d, f), ROOT) for d, _, fs in os.walk(ROOT) for f in fs
        if "third_party" not in d and "/build" not in d]
    return [n for n in names if n.endswith(exts) or (not exts and "." not in os.path.basename(n))]


def test_no_report_error():
    """Components.utils.reportError no longer exists in Firefox; use console.error."""
    for name in files(".js", ".mjs", ".cfg"):
        with open(os.path.join(ROOT, name)) as f:
            assert "reportError" not in f.read(), name


def test_no_self_matching_pkill():
    """pgrep -f / pkill -f with a pattern from your own command line kills
    the calling shell."""
    scripts = files(".sh") + [n for n in ("hybrid",) if os.path.exists(os.path.join(ROOT, n))]
    for name in scripts:
        with open(os.path.join(ROOT, name)) as f:
            assert not re.search(r"\b(pgrep|pkill)\b[^\n]*\s-f\b", f.read()), name


def test_shell_loaded_from_resource_url():
    """Privileged scripts can't be loaded from file:// URLs."""
    with open(os.path.join(ROOT, "shell/hybrid.cfg")) as f:
        cfg = f.read()
    assert 'setSubstitution("hybrid"' in cfg and "resource://hybrid/hybrid-browser.js" in cfg


def test_chromium_host_finds_libcef_next_to_itself():
    """Without an $ORIGIN rpath, libcef.so loads from the CEF source folder
    and ICU data isn't found."""
    if not os.access(HOSTS["blink"], os.X_OK):
        pytest.skip("blink host isn't built")
    dyn = subprocess.run(["readelf", "-d", HOSTS["blink"]], capture_output=True, text=True).stdout
    paths = re.search(r"\((?:RUNPATH|RPATH)\)\s+Library r(?:un)?path: \[(.*)\]", dyn)
    assert paths and "$ORIGIN" in paths.group(1).split(":"), dyn
    ldd = subprocess.run(["ldd", HOSTS["blink"]], capture_output=True, text=True).stdout
    libcef = next(line for line in ldd.splitlines() if "libcef.so" in line)
    assert os.path.dirname(HOSTS["blink"]) in libcef, libcef
