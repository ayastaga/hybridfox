"""Launch ./hybrid with a temporary profile and drive it through Marionette.

Used by tests/test_browser.py and scripts/bench.py.
"""
import os
import shutil
import signal
import socket
import subprocess
import time

from hostlib import ROOT
from marionette import Marionette

ARTIFACTS = os.environ.get("HYBRID_TEST_ARTIFACTS", os.path.join(os.path.dirname(__file__), "artifacts"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FIREFOX = os.path.join(ROOT, "third_party/firefox/firefox")
PROFILE_DIRNAME = "hybridfox"

PREFS = """
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.startup.page", 0);
user_pref("startup.homepage_welcome_url", "");
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("datareporting.policy.firstRunURL", "");
user_pref("browser.tabs.warnOnClose", false);
user_pref("devtools.console.stdout.chrome", true);
user_pref("browser.dom.window.dump.enabled", true);
user_pref("marionette.port", {port});
"""


class Browser:
    def __init__(self, tmp_path, name, env=None, profile_dirname=PROFILE_DIRNAME, prefs=""):
        os.makedirs(ARTIFACTS, exist_ok=True)
        self.data = tmp_path / "data"
        profile = self.data / profile_dirname / "firefox-profile"
        profile.mkdir(parents=True)
        port = free_port()
        (profile / "user.js").write_text(PREFS.replace("{port}", str(port)) + prefs)
        self.log_path = os.path.join(ARTIFACTS, f"{name}-browser.log")
        self.log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [os.path.join(ROOT, "hybridfox"), "--marionette", "about:blank"],
            env={
                **os.environ,
                "XDG_DATA_HOME": str(self.data),
                "HYBRID_TEST": "1",
                # Firefox only lets Marionette into the chrome context with this.
                "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1",
                "HYBRID_DEBUG": "1",
                "HYBRID_NO_SANDBOX": "1",
                "HYBRID_CHROMIUM_FLAGS": os.environ.get("HYBRID_CHROMIUM_FLAGS", "--disable-gpu"),
                **(env or {}),
            },
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.m = Marionette(port)
        self.m.start()
        self.m.wait("return !!window.HybridTest", what="the hybrid layer to load")

    # Shorthands for window.HybridTest
    def tabs(self):
        return self.m.js("return HybridTest.tabs()")

    def tab(self, index):
        return self.tabs()[index]

    def host(self):
        return self.m.js("return HybridTest.host()")

    def wait_tab(self, index, pred, timeout=30, what="tab state"):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            tabs = self.tabs()
            last = tabs[index] if index < len(tabs) else None
            if last and pred(last):
                return last
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}; tab {index}: {last}")

    def log_text(self):
        self.log.flush()
        with open(self.log_path) as f:
            return f.read()

    def quit(self):
        try:
            self.m.js("Services.startup.quit(Ci.nsIAppStartup.eForceQuit)")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            # Only this test's process group: never match by name.
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        self.log.close()
        # Profiles are 50-100 MB, and /tmp is often a small RAM disk.
        shutil.rmtree(self.data, ignore_errors=True)
