import os
import socket
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from hostlib import HOSTS, Host, Parent, Site  # noqa: E402

ARTIFACTS = os.environ.get("HYBRID_TEST_ARTIFACTS", os.path.join(os.path.dirname(__file__), "artifacts"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session", autouse=True)
def _needs_display():
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display: run the tests under xvfb-run")
    os.makedirs(ARTIFACTS, exist_ok=True)


@pytest.fixture(scope="session")
def site():
    s = Site(free_port())
    yield s
    s.stop()


@pytest.fixture
def parent():
    marker = "hybrid-test-" + uuid.uuid4().hex[:10]
    p = Parent(marker)
    p.marker = marker
    yield p
    p.close()


@pytest.fixture(params=["blink", "webkit"])
def engine(request):
    if not os.access(HOSTS[request.param], os.X_OK):
        pytest.skip(f"{request.param} host isn't built ({HOSTS[request.param]})")
    return request.param


@pytest.fixture
def blink():
    if not os.access(HOSTS["blink"], os.X_OK):
        pytest.skip(f"blink host isn't built ({HOSTS['blink']})")
    return "blink"


@pytest.fixture
def start_host(request, parent, site):
    """start_host(engine, env=None) -> a Host attached to |parent| as window 1."""
    hosts = []

    def start(engine, env=None):
        name = request.node.name.replace("/", "_").replace("[", "-").replace("]", "")
        h = Host(engine, os.path.join(ARTIFACTS, f"{name}-{engine}.log"), env)
        hosts.append(h)
        h.wait_event("ready", timeout=30)
        h.attach(1, parent.marker)
        site.reset()
        return h

    yield start
    for h in hosts:
        h.quit()
