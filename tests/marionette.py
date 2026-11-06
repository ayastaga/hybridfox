"""A minimal Marionette client (Firefox's remote-control protocol).

Messages are "length:json" over TCP. Commands are [0, id, name, params];
replies are [1, id, error, result]. Only what the tests need is here:
NewSession, SetContext and ExecuteScript / ExecuteAsyncScript, plus
Addon:Install.
"""
import json
import socket
import time


class MarionetteError(Exception):
    pass


class Marionette:
    def __init__(self, port, host="127.0.0.1", timeout=60):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection((host, port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.3)
        self.sock.settimeout(120)
        self.buf = b""
        self.next_id = 1
        self.hello = self._recv()

    def _recv(self):
        while b":" not in self.buf:
            self._fill()
        length, _, rest = self.buf.partition(b":")
        self.buf = rest
        n = int(length)
        while len(self.buf) < n:
            self._fill()
        msg, self.buf = self.buf[:n], self.buf[n:]
        return json.loads(msg)

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("Marionette closed the connection")
        self.buf += chunk

    def command(self, name, params=None):
        msg_id = self.next_id
        self.next_id += 1
        data = json.dumps([0, msg_id, name, params or {}]).encode()
        self.sock.sendall(str(len(data)).encode() + b":" + data)
        while True:
            reply = self._recv()
            if reply[0] == 1 and reply[1] == msg_id:
                break
        if reply[2]:
            err = reply[2]
            raise MarionetteError(f"{name}: {err.get('error')}: {err.get('message')}\n{err.get('stacktrace', '')}")
        return reply[3]

    def start(self):
        self.command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"acceptInsecureCerts": True}}})
        self.command("Marionette:SetContext", {"value": "chrome"})

    def js(self, script, *args, timeout=30):
        """Run |script| in the browser window (chrome context); return its value."""
        self.command("WebDriver:SetTimeouts", {"script": int(timeout * 1000)})
        return self.command("WebDriver:ExecuteScript", {"script": script, "args": list(args)}).get("value")

    def js_async(self, script, *args, timeout=30):
        """Like js(), for a script that ends by calling resolve(value)
        (the last argument)."""
        self.command("WebDriver:SetTimeouts", {"script": int(timeout * 1000)})
        wrapped = "const resolve = arguments[arguments.length - 1];\n" + script
        return self.command("WebDriver:ExecuteAsyncScript", {"script": wrapped, "args": list(args)}).get("value")

    def wait(self, script, *args, timeout=30, what="condition"):
        """Poll |script| until it returns something truthy; return that."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.js(script, *args)
            except MarionetteError as e:
                last = e
            else:
                if last:
                    return last
            time.sleep(0.25)
        raise AssertionError(f"timed out waiting for {what}; last value: {last}")

    def install_addon(self, path, temporary=True):
        return self.command("Addon:Install", {"path": path, "temporary": temporary})

    def close(self):
        try:
            self.command("WebDriver:DeleteSession")
        except Exception:
            pass
        self.sock.close()
