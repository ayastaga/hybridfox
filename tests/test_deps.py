"""scripts/deps.py fetch, offline: archives served from a local folder.

Each test copies deps.py into a scratch project (so the real third_party/
is never touched) with a versions.json pointing at small stand-in archives.
"""
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from hostlib import ROOT

ARCH = "x86_64" if platform.machine() in ("x86_64", "amd64") else "aarch64"


def make_tar(path, top, files, mode):
    staging = path.parent / ("src-" + path.name)
    for rel, content in files.items():
        f = staging / top / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    with tarfile.open(path, mode) as t:
        t.add(staging / top, arcname=top)
    shutil.rmtree(staging)


def digest(path, algo):
    return hashlib.new(algo, path.read_bytes()).hexdigest()


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "scripts/deps.py"), proj / "scripts/deps.py")
    cdn = tmp_path / "cdn"
    cdn.mkdir()

    def publish(ff_marker="ff-1", cef_marker="cef-1", cef_version="1.0.0+gabc+chromium-1.0.0.0"):
        # Firefox's archive holds firefox/firefox: a file named like its folder.
        ff = cdn / f"firefox-{ff_marker}.tar.xz"
        make_tar(ff, "firefox", {"firefox": ff_marker, "application.ini": "Version=1\n"}, "w:xz")
        cef_name = f"cef_binary_{cef_version}_minimal.tar.bz2"
        cef = cdn / cef_name
        make_tar(cef, cef_name[: -len("_minimal.tar.bz2")], {"Release/libcef.so": cef_marker}, "w:bz2")
        versions = {
            "firefox": {"version": ff_marker, "files": {ARCH: {"url": ff.as_uri(), "sha256": digest(ff, "sha256")}}},
            "cef": {"version": cef_version, "files": {ARCH: {"name": cef_name, "sha1": digest(cef, "sha1")}}},
        }
        (proj / "versions.json").write_text(json.dumps(versions))
        return versions

    def fetch():
        return subprocess.run(
            ["python3", str(proj / "scripts/deps.py"), "fetch"], capture_output=True, text=True,
            env={**os.environ, "HYBRID_CEF_CDN": cdn.as_uri() + "/"},
        )

    return SimpleNamespace(root=proj, publish=publish, fetch=fetch, cdn=cdn)


def test_fetch_verifies_and_unpacks(project):
    project.publish()
    r = project.fetch()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (project.root / "third_party/firefox/firefox").read_text() == "ff-1"
    assert (project.root / "third_party/cef/Release/libcef.so").read_text() == "cef-1"
    assert (project.root / "third_party/firefox/.pinned").read_text().startswith("ff-1 ")
    assert not [p for p in os.listdir(project.root / "third_party") if p.startswith(".")]  # no leftovers


def test_fetch_skips_what_is_up_to_date(project):
    project.publish()
    assert project.fetch().returncode == 0
    for f in project.cdn.iterdir():
        f.unlink()  # nothing left to download from
    r = project.fetch()
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("up to date") == 2


def test_checksum_mismatch_is_refused(project):
    v = project.publish()
    v["firefox"]["files"][ARCH]["sha256"] = "0" * 64
    (project.root / "versions.json").write_text(json.dumps(v))
    r = project.fetch()
    assert r.returncode != 0
    assert "checksum mismatch" in r.stderr
    assert not (project.root / "third_party/firefox").exists()  # nothing unpacked


def test_cef_change_wipes_engine_build(project):
    first = project.publish()
    assert project.fetch().returncode == 0
    build = project.root / "engine/build/out"
    build.mkdir(parents=True)
    (build / "chromium-host").write_text("old")
    # A new CEF pin; Firefox's pin stays exactly as it was.
    second = project.publish(cef_marker="cef-2", cef_version="2.0.0+gdef+chromium-2.0.0.0")
    second["firefox"] = first["firefox"]
    (project.root / "versions.json").write_text(json.dumps(second))
    r = project.fetch()
    assert r.returncode == 0, r.stdout + r.stderr
    assert (project.root / "third_party/cef/Release/libcef.so").read_text() == "cef-2"
    assert not (project.root / "engine/build").exists()
    assert "Firefox ff-1: up to date" in r.stdout  # unchanged pin: not re-downloaded
