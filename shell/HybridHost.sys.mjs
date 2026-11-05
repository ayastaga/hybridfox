/* Shared by every browser window in this Firefox session.
 *
 *  - Engines: one helper process per non-Gecko engine (Blink via CEF, WebKit
 *    via WebKitGTK), started the first time a tab needs it. All speak the
 *    same line protocol.
 *  - Windows and tabs: which window and which engine own each tab.
 *  - Cookies: Firefox's jar is the master copy. Every running engine mirrors
 *    it, and changes made in any engine flow back to Firefox and on to the
 *    other engines. "private" jars mirror Firefox's private-browsing cookies
 *    and are discarded when the last private window closes.
 *  - Protection and extensions: engines ask before loads; Firefox answers
 *    with its own Safe Browsing and tracking protection, and, for Chromium
 *    requests, with the verdict of installed request-filtering extensions
 *    (uBlock Origin, Privacy Badger, ...), obtained through the tab's
 *    placeholder page (see hybrid-browser.js, probe()).
 */
import { Subprocess } from "resource://gre/modules/Subprocess.sys.mjs";
import { setTimeout, clearTimeout, setInterval } from "resource://gre/modules/Timer.sys.mjs";
import { HybridPrivacy } from "resource://hybrid/HybridPrivacy.sys.mjs";

const env = Cc["@mozilla.org/process/environment;1"].getService(Ci.nsIEnvironment);
const DEBUG = !!env.get("HYBRID_DEBUG");
const log = (...a) => console.log("[hybrid]", ...a);
const debug = (...a) => DEBUG && console.log("[hybrid]", ...a);

export const ENGINES = {
  blink: { label: "Blink", product: "Chromium", pathEnv: "HYBRID_HOST" },
  webkit: { label: "WebKit", product: "WebKit", pathEnv: "HYBRID_WEBKIT_HOST" },
};

const COOKIE_CMDS = new Set(["cookies-replace", "cookie-set", "cookie-del", "cookies-flush"]);
// Commands that never wait behind queued tab commands.
const BYPASS = new Set([
  ...COOKIE_CMDS, "attach", "detach", "quit", "unfocus", "dialog-reply", "private-end",
  "classified", "protection-reset", "bridge-mode",
]);

const windows = new Map(); // winId -> { window, handler, isPrivate, marker, markerTimer }
const tabOwner = new Map(); // tabId -> { winId, engine }
const flushWaiters = new Map();
let shuttingDown = false; // Firefox is quitting: engine exits are expected
let nextWinId = 1;
let nextTabId = 1;
let nextReq = 1;

// ---------------------------------------------------------------- engines --

class Engine {
  constructor(name) {
    this.name = name;
    this.info = ENGINES[name];
    this.proc = null;
    this.starting = false;
    this.ready = false;
    this.failure = null;
    this.pending = [];
    this.attached = new Set();
    this.attaching = new Set();
    this.jars = { normal: { started: false, ready: false }, private: { started: false, ready: false } };
    this.quitting = false; // we asked it to quit
    this.crashes = []; // when it exited unexpectedly (ms), for the restart limit
    this.restarts = 0;
  }

  get available() {
    return !!env.get(this.info.pathEnv) && !this.failure;
  }

  canSend(obj) {
    if (!this.proc || !this.ready) return false;
    if (BYPASS.has(obj.cmd)) return true;
    if (obj.win !== undefined && !this.attached.has(obj.win)) return false;
    if (obj.cmd === "create" && !this.jars[obj.jar || "normal"].ready) return false;
    return true;
  }

  write(obj) {
    if (DEBUG && !COOKIE_CMDS.has(obj.cmd) && obj.cmd !== "classified") debug(`-> ${this.name}`, obj);
    this.proc.stdin.write(JSON.stringify(obj) + "\n").catch(e => log("write failed", e));
  }

  send(obj) {
    if (this.failure) return;
    this.start();
    // Keep tab commands in order: once one is queued, later ones queue too.
    if (this.canSend(obj) && (BYPASS.has(obj.cmd) || this.pending.length === 0)) {
      this.write(obj);
    } else {
      this.pending.push(obj);
    }
  }

  flush() {
    for (let i = 0; i < this.pending.length; ) {
      const obj = this.pending[i];
      if (this.canSend(obj)) {
        this.pending.splice(i, 1);
        this.write(obj);
      } else if (BYPASS.has(obj.cmd)) {
        i++;
      } else {
        break; // a blocked tab command holds back the ones after it
      }
    }
  }

  async start() {
    if (this.proc || this.starting || this.failure) return;
    const path = env.get(this.info.pathEnv);
    if (!path) {
      this.fail(`The ${this.info.label} engine isn't installed (${this.info.pathEnv} is not set).`);
      return;
    }
    this.starting = true;
    this.quitting = false;
    // Engines render at exactly Firefox's scale, so all tabs match.
    const anyWindow = [...windows.values()][0]?.window;
    const scale = String(Math.round((anyWindow?.devicePixelRatio || 1) * 1000) / 1000);
    try {
      this.proc = await Subprocess.call({
        command: path,
        arguments: [],
        environment: { HYBRID_SCALE: scale },
        environmentAppend: true,
        stderr: "pipe",
      });
    } catch (e) {
      this.starting = false;
      this.fail(`Could not start the ${this.info.label} engine: ${e}`);
      return;
    }
    this.starting = false;
    log(`${this.info.label} engine started at scale`, scale);
    readLines(this.proc.stdout, line => onEngineLine(this, line));
    readLines(this.proc.stderr, line => debug(`${this.name}:`, line));
    const proc = this.proc;
    proc.wait().then(({ exitCode }) => {
      log(`${this.info.label} engine exited with code`, exitCode);
      if (this.proc === proc) this.onExit(exitCode);
    });
  }

  // Forget everything that belonged to the process that just exited.
  reset() {
    this.proc = null;
    this.ready = false;
    this.attached.clear();
    this.attaching.clear();
    // Queued commands were for the old process; windows re-send what the
    // new one needs (onEngineRestart).
    this.pending = [];
    for (const jar of Object.values(this.jars)) {
      jar.started = false;
      jar.ready = false;
    }
    for (const [key, done] of flushWaiters) {
      if (key.startsWith(this.name + ":")) {
        flushWaiters.delete(key);
        done();
      }
    }
  }

  // The process exited. Expected when we asked it to quit or Firefox is
  // shutting down; otherwise it crashed (or was killed): start a new one
  // and have every window reopen its tabs there, at their last address and
  // with the same tab ids. A crash loop isn't worth chasing: after
  // MAX_CRASHES within CRASH_WINDOW_MS, give up and mark its tabs dead.
  onExit(exitCode) {
    const expected = this.quitting || shuttingDown;
    this.reset();
    if (expected || this.failure) return;
    const now = Date.now();
    this.crashes = this.crashes.filter(t => now - t < CRASH_WINDOW_MS);
    this.crashes.push(now);
    if (this.crashes.length >= MAX_CRASHES) {
      this.fail(`The ${this.info.label} engine stopped unexpectedly ${this.crashes.length} times in ` +
        `${CRASH_WINDOW_MS / 60000} minutes, so it won't be restarted. Restart the browser to use ` +
        `${this.info.label} tabs again.`);
      return;
    }
    this.restarts++;
    log(`${this.info.label} engine stopped unexpectedly (exit code ${exitCode}); restarting it`);
    this.start();
    for (const [winId, w] of windows) {
      try {
        w.handler.onEngineRestart(this.name);
      } catch (e) {
        console.error("[hybrid] reopening tabs failed in window", winId, e);
      }
    }
  }

  fail(msg) {
    this.failure = msg;
    console.error("[hybrid]", msg);
    for (const w of windows.values()) {
      try {
        w.handler.onEngineFail(this.name, msg);
      } catch (e) {}
    }
  }

  attach(winId) {
    if (!this.ready || this.attached.has(winId) || this.attaching.has(winId)) return;
    const entry = windows.get(winId);
    if (!entry) return;
    this.attaching.add(winId);
    startMarker(entry, winId);
    this.write({ cmd: "attach", win: winId, marker: entry.marker });
  }
}

const MAX_CRASHES = 3;
const CRASH_WINDOW_MS = 2 * 60 * 1000;

const engines = Object.fromEntries(Object.keys(ENGINES).map(n => [n, new Engine(n)]));

Services.obs.addObserver(() => {
  shuttingDown = true;
}, "quit-application-granted");

async function readLines(pipe, onLine) {
  let buf = "";
  for (;;) {
    const chunk = await pipe.readString();
    if (!chunk) break;
    buf += chunk;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      onLine(buf.slice(0, i));
      buf = buf.slice(i + 1);
    }
  }
}

// --------------------------------------------------------- window markers --
// Engines find a window's X11 window by its title. While any engine is
// attaching, the window's title is a unique marker; Firefox may retitle the
// window meanwhile (a tab's title changed), so it is reapplied until done.

function startMarker(entry, winId) {
  entry.marker = entry.marker || `hybrid-attach-${winId}-${Math.random().toString(36).slice(2)}`;
  const apply = () => {
    try {
      entry.window.document.title = entry.marker;
    } catch (e) {}
  };
  apply();
  if (!entry.markerTimer) entry.markerTimer = entry.window.setInterval(apply, 150);
}

function stopMarkerIfDone(entry, winId) {
  if (Object.values(engines).some(e => e.attaching.has(winId))) return;
  if (entry.markerTimer) {
    entry.window.clearInterval(entry.markerTimer);
    entry.markerTimer = null;
  }
  try {
    entry.window.gBrowser.updateTitlebar();
  } catch (e) {}
}

function deliver(winId, ev) {
  const entry = windows.get(winId);
  if (!entry) return;
  try {
    entry.handler.onEvent(ev);
  } catch (e) {
    console.error("[hybrid] window handler failed", e);
  }
}

// ----------------------------------------------------------------- events --

function onEngineLine(engine, line) {
  if (!line.startsWith("@@")) {
    debug(`${engine.name}:`, line);
    return;
  }
  let ev;
  try {
    ev = JSON.parse(line.slice(2));
  } catch (e) {
    log("bad event", line);
    return;
  }
  ev.engine = engine.name;
  if (DEBUG && !ev.event.startsWith("cookie") && ev.event !== "classify") debug(`<- ${engine.name}`, ev);

  switch (ev.event) {
    case "ready":
      engine.ready = true;
      CookieSync.start(engine, "normal");
      if ([...windows.values()].some(w => w.isPrivate)) CookieSync.start(engine, "private");
      engine.write({ cmd: "bridge-mode", extensions: Bridge.active });
      for (const winId of windows.keys()) engine.attach(winId);
      engine.flush();
      return;
    case "attached": {
      engine.attaching.delete(ev.win);
      engine.attached.add(ev.win);
      const entry = windows.get(ev.win);
      if (entry) stopMarkerIfDone(entry, ev.win);
      log(`window ${ev.win} attached to ${engine.info.label}`);
      engine.flush();
      return;
    }
    case "error":
      if (ev.win !== undefined) {
        engine.attaching.delete(ev.win);
        const entry = windows.get(ev.win);
        if (entry) {
          stopMarkerIfDone(entry, ev.win);
          entry.handler.onEngineFail(engine.name, ev.msg);
        }
      } else if (ev.id !== undefined && tabOwner.has(ev.id)) {
        deliver(tabOwner.get(ev.id).winId, ev);
      } else {
        engine.fail(ev.msg);
      }
      return;
    case "log":
      log(`${engine.name}:`, ev.msg);
      return;
    case "cookies-ready":
      engine.jars[ev.jar].ready = true;
      log(`${engine.info.label} ${ev.jar} cookie jar ready:`, ev.count, "cookies mirrored");
      engine.flush();
      return;
    case "cookie-set":
    case "cookie-del":
      CookieSync.fromEngine(engine, ev, ev.event === "cookie-del");
      return;
    case "cookies-flushed": {
      const done = flushWaiters.get(`${engine.name}:${ev.req}`);
      flushWaiters.delete(`${engine.name}:${ev.req}`);
      if (done) done();
      return;
    }
    case "classify":
      classify(engine, ev);
      return;
    case "check-timeout":
      // The engine gave up waiting for a verdict (see classify()).
      log(`${engine.info.label}: no verdict in time for ${ev.kind}`, ev.url, ev.allowed ? "(allowed)" : "(blocked)");
      break; // also delivered to the tab's window below
    case "private-ended":
    case "focused":
    case "unfocused":
      return;
    case "popup": {
      // A popup belongs to its opener's window.
      const owner = tabOwner.get(ev.opener);
      if (!owner) return;
      tabOwner.set(ev.id, { winId: owner.winId, engine: engine.name });
      deliver(owner.winId, ev);
      return;
    }
  }
  if (ev.id !== undefined && tabOwner.has(ev.id)) {
    const owner = tabOwner.get(ev.id);
    deliver(owner.winId, ev);
    if (ev.event === "closed") tabOwner.delete(ev.id);
  }
}

// ------------------------------------------- protection + extension bridge --

const Bridge = {
  active: false,
  // Extensions that can block or rewrite requests through webRequest.
  // Firefox's own built-in add-ons are privileged and skipped.
  compute() {
    try {
      return WebExtensionPolicy.getActiveExtensions().some(
        p => !p.isPrivileged && (p.hasPermission("webRequestBlocking") || p.hasPermission("webRequest"))
      );
    } catch (e) {
      return false;
    }
  },
  refresh() {
    const now = this.compute();
    if (now === this.active) return;
    this.active = now;
    log(now ? "request-filtering extensions found: Chromium requests will go through them" : "no request-filtering extensions");
    for (const e of Object.values(engines)) {
      if (e.ready) {
        e.write({ cmd: "bridge-mode", extensions: now });
        e.write({ cmd: "protection-reset" });
      }
    }
  },
};
Bridge.active = Bridge.compute();
setInterval(() => Bridge.refresh(), 3000);

// When a check can't produce an answer, subresources are blocked ("fail
// closed") so a broken check can't silently let trackers through; page loads
// are allowed so browsing never becomes impossible. HYBRID_FAIL_OPEN=1 allows
// both, as earlier versions did.
const FAIL_OPEN = !!env.get("HYBRID_FAIL_OPEN");
const NO_VERDICT = "no verdict";
const noVerdictFor = ev => (ev.kind === "resource" && !FAIL_OPEN ? NO_VERDICT : "");

async function classify(engine, ev) {
  let reason = "";
  let reply = {};
  // Firefox's own verdicts depend only on the sites involved, so the engine
  // may reuse them; extension verdicts depend on the exact request.
  let cache = true;
  try {
    reason = await HybridPrivacy.classify(ev);
  } catch (e) {
    log("privacy check failed for", ev.url, e);
    reason = noVerdictFor(ev);
    cache = false;
  }
  if (!reason && ev.kind === "resource" && Bridge.active && tabOwner.has(ev.id)) {
    cache = false;
    const owner = tabOwner.get(ev.id);
    const entry = windows.get(owner.winId);
    try {
      const r = await entry?.handler.probe(ev);
      if (!r || r.noVerdict) {
        log("extension check gave no verdict for", ev.url, r?.note || "");
        reason = noVerdictFor(ev);
      } else if (r.block) reason = "extension";
      else if (r.redirectBody !== undefined) reply = { redirectBody: r.redirectBody, redirectMime: r.redirectMime || "" };
      else if (r.redirectURL) reply = { redirectURL: r.redirectURL };
    } catch (e) {
      log("extension check failed for", ev.url, e);
      reason = noVerdictFor(ev);
    }
  }
  if (reason) debug("blocking", ev.kind, ev.url, `(${reason})`);
  if (engine.proc) engine.write({ cmd: "classified", req: ev.req, reason, cache, ...reply });
}

HybridPrivacy.watch(() => {
  for (const e of Object.values(engines)) if (e.ready) e.write({ cmd: "protection-reset" });
});

// ------------------------------------------------- cookies across engines --

const CookieSync = (() => {
  const N = Ci.nsICookieNotification;
  const SS = {
    none: Ci.nsICookie.SAMESITE_NONE,
    lax: Ci.nsICookie.SAMESITE_LAX,
    strict: Ci.nsICookie.SAMESITE_STRICT,
    unset: Ci.nsICookie.SAMESITE_UNSET ?? Ci.nsICookie.SAMESITE_NONE,
  };
  const SS_NAME = new Map(Object.entries(SS).map(([k, v]) => [v, k]));
  const FAR_FUTURE = 2 ** 31 - 1; // seconds; the expiry slot for session cookies
  // jar + key -> { value, t, source } for changes that came from an engine
  const recent = new Map();
  let expiryInMs = null;
  let observing = false;

  const OA = jar => (jar === "private" ? { privateBrowsingId: 1 } : {});
  const keyOf = (jar, c) => jar + "\t" + c.domain + "\t" + c.path + "\t" + c.name;

  // Which shared jar a Firefox cookie belongs to; null for container,
  // partitioned (third-party) and first-party-isolated cookies.
  function jarOf(c) {
    const oa = c.originAttributes || {};
    if (oa.userContextId || oa.partitionKey || oa.firstPartyDomain) return null;
    return oa.privateBrowsingId ? "private" : "normal";
  }

  function allCookies(jar) {
    const pattern = JSON.stringify({ privateBrowsingId: jar === "private" ? 1 : 0 });
    return Services.cookies.getCookiesWithOriginAttributes(pattern).filter(c => jarOf(c) === jar);
  }

  // Firefox has moved cookie expiry from seconds to milliseconds over time.
  function detectUnits() {
    if (expiryInMs !== null) return;
    const probe = allCookies("normal").find(c => !c.isSession);
    if (probe) {
      expiryInMs = probe.expiry > 1e11;
    } else {
      const soon = Math.floor(Date.now() / 1000) + 3600;
      try {
        expiryInMs = false;
        addRaw("normal", "hybrid-probe.invalid", "/", "hybrid-probe", "1", false, false, false, soon, SS.lax);
        const back = allCookies("normal").find(c => c.name === "hybrid-probe");
        expiryInMs = back ? back.expiry > 1e11 : true;
        Services.cookies.remove("hybrid-probe.invalid", "hybrid-probe", "/", {});
      } catch (e) {
        expiryInMs = true;
      }
    }
    log("cookie expiry units:", expiryInMs ? "milliseconds" : "seconds");
  }

  function addRaw(jar, host, path, name, value, secure, httpOnly, session, expirySecs, sameSite) {
    const exp = expiryInMs ? Math.round(expirySecs * 1000) : Math.round(expirySecs);
    const schemes = secure ? Ci.nsICookie.SCHEME_HTTPS : Ci.nsICookie.SCHEME_HTTP | Ci.nsICookie.SCHEME_HTTPS;
    return Services.cookies.add(host, path, name, value, secure, httpOnly, session, exp, OA(jar), sameSite, schemes, false);
  }

  function toWire(c) {
    let exp = c.expiry;
    if (exp > 1e11) exp /= 1000;
    return {
      domain: c.host,
      path: c.path,
      name: c.name,
      value: c.value,
      secure: c.isSecure,
      httpOnly: c.isHttpOnly,
      session: c.isSession,
      expires: c.isSession ? 0 : exp,
      sameSite: SS_NAME.get(c.sameSite) || "unset",
    };
  }

  function start(engine, jar) {
    if (engine.jars[jar].started) return;
    engine.jars[jar].started = true;
    engine.jars[jar].ready = false;
    detectUnits();
    observe();
    engine.write({ cmd: "cookies-replace", jar, cookies: allCookies(jar).map(toWire) });
  }

  // Did this change come from |engine| itself (so it shouldn't go back)?
  function isEchoFor(engineName, jar, w, deleted) {
    const r = recent.get(keyOf(jar, w));
    return (
      r && r.source === engineName && Date.now() - r.t < 3000 &&
      (deleted ? r.value === null : r.value === w.value)
    );
  }

  const observer = {
    observe(subject, topic) {
      const topicJar = topic === "private-cookie-changed" ? "private" : "normal";
      let n;
      try {
        n = subject.QueryInterface(N);
      } catch (e) {
        return;
      }
      const one = (c, deleted) => {
        if (!c || jarOf(c) !== topicJar) return;
        const w = toWire(c);
        for (const engine of Object.values(engines)) {
          if (!engine.ready || !engine.jars[topicJar].started) continue;
          if (isEchoFor(engine.name, topicJar, w, deleted)) continue;
          engine.send({ cmd: deleted ? "cookie-del" : "cookie-set", jar: topicJar, ...w });
        }
      };
      switch (n.action) {
        case N.COOKIE_ADDED:
        case N.COOKIE_CHANGED:
          one(n.cookie, false);
          break;
        case N.COOKIE_DELETED:
          one(n.cookie, true);
          break;
        case N.COOKIES_BATCH_DELETED:
          try {
            for (const c of n.batchDeletedCookies || []) one(c.QueryInterface(Ci.nsICookie), true);
          } catch (e) {
            log("batch delete not mirrored", e);
          }
          break;
        case N.ALL_COOKIES_CLEARED:
          for (const engine of Object.values(engines)) {
            if (engine.ready && engine.jars[topicJar].started) {
              engine.send({ cmd: "cookies-replace", jar: topicJar, cookies: [] });
            }
          }
          break;
      }
    },
  };

  function observe() {
    if (observing) return;
    observing = true;
    Services.obs.addObserver(observer, "cookie-changed");
    Services.obs.addObserver(observer, "private-cookie-changed");
  }

  // A change made inside an engine: apply it to Firefox. Firefox's
  // notification then relays it to the other engines, but not back.
  function fromEngine(engine, ev, deleted) {
    const jar = ev.jar || "normal";
    if (!engine.jars[jar].started) return;
    recent.set(keyOf(jar, ev), { value: deleted ? null : ev.value, t: Date.now(), source: engine.name });
    try {
      if (deleted) {
        Services.cookies.remove(ev.domain, ev.name, ev.path, OA(jar));
      } else {
        addRaw(
          jar, ev.domain, ev.path, ev.name, ev.value, ev.secure, ev.httpOnly, ev.session,
          ev.session ? FAR_FUTURE : ev.expires, SS[ev.sameSite] ?? SS.unset
        );
      }
    } catch (e) {
      log("could not mirror cookie", ev.name, "for", ev.domain, e);
    }
    if (recent.size > 4000) {
      const cutoff = Date.now() - 10000;
      for (const [k, v] of recent) if (v.t < cutoff) recent.delete(k);
    }
  }

  function endPrivate() {
    for (const engine of Object.values(engines)) {
      engine.jars.private.started = false;
      engine.jars.private.ready = false;
    }
    for (const k of recent.keys()) if (k.startsWith("private\t")) recent.delete(k);
  }

  return { start, fromEngine, endPrivate };
})();

// ------------------------------------------------------------ public API --

export const HybridHost = {
  engines: ENGINES,

  failureOf(name) {
    return engines[name]?.failure || (engines[name]?.available ? null : `The ${ENGINES[name].label} engine isn't installed.`);
  },

  // A browser window joins. |handler| receives onEvent(event),
  // onEngineFail(engine, message), onEngineRestart(engine) (the engine
  // crashed and a new process is starting: reopen its tabs) and
  // probe(request) -> verdict.
  registerWindow(window, handler, isPrivate) {
    const winId = nextWinId++;
    windows.set(winId, { window, handler, isPrivate });
    for (const engine of Object.values(engines)) {
      if (!engine.ready) continue;
      if (isPrivate) CookieSync.start(engine, "private");
      engine.attach(winId);
    }
    return winId;
  },

  unregisterWindow(winId) {
    const entry = windows.get(winId);
    if (!entry) return;
    try {
      if (entry.markerTimer) entry.window.clearInterval(entry.markerTimer);
    } catch (e) {}
    windows.delete(winId);
    for (const engine of Object.values(engines)) {
      engine.pending = engine.pending.filter(o => o.win !== winId);
      engine.attached.delete(winId);
      engine.attaching.delete(winId);
      if (engine.proc) engine.write({ cmd: "detach", win: winId });
    }
    if (entry.isPrivate && ![...windows.values()].some(w => w.isPrivate)) {
      // Last private window: the private session is over everywhere.
      CookieSync.endPrivate();
      for (const engine of Object.values(engines)) if (engine.proc) engine.write({ cmd: "private-end" });
      log("private session ended; private engine profiles discarded");
    }
    if (windows.size === 0) {
      for (const engine of Object.values(engines)) {
        if (!engine.proc) continue;
        engine.quitting = true;
        engine.write({ cmd: "quit" });
      }
    }
  },

  // Make sure |name| is running and attached to this window (and has the
  // private jar if this window is private).
  use(name, winId) {
    const engine = engines[name];
    if (!engine || engine.failure) return;
    engine.start();
    const entry = windows.get(winId);
    if (engine.ready) {
      if (entry?.isPrivate) CookieSync.start(engine, "private");
      engine.attach(winId);
    }
  },

  allocTabId(winId, engine) {
    const id = nextTabId++;
    tabOwner.set(id, { winId, engine });
    return id;
  },

  releaseTab(id) {
    tabOwner.delete(id);
  },

  send(engine, obj) {
    engines[engine]?.send(obj);
  },

  // A snapshot of the session, for tests and debugging.
  debugState() {
    return {
      failOpen: FAIL_OPEN,
      bridgeActive: Bridge.active,
      windows: [...windows.entries()].map(([id, w]) => ({ id, isPrivate: !!w.isPrivate })),
      tabs: [...tabOwner.entries()].map(([id, o]) => ({ id, ...o })),
      engines: Object.fromEntries(Object.values(engines).map(e => [e.name, {
        running: !!e.proc,
        pid: e.proc?.pid ?? null,
        crashes: e.crashes.length,
        restarts: e.restarts,
        ready: e.ready,
        failure: e.failure,
        attached: [...e.attached],
        pending: e.pending.length,
        jars: e.jars,
      }])),
    };
  },

  // Ask an engine for its latest cookies and wait until Firefox has them.
  flushCookies(name, jar = "normal", timeoutMs = 2000) {
    const engine = engines[name];
    if (!engine?.proc || !engine.ready || !engine.jars[jar].ready) return Promise.resolve();
    return new Promise(resolve => {
      const req = nextReq++;
      const key = `${name}:${req}`;
      const timer = setTimeout(() => {
        flushWaiters.delete(key);
        resolve();
      }, timeoutMs);
      flushWaiters.set(key, () => {
        clearTimeout(timer);
        resolve();
      });
      engine.send({ cmd: "cookies-flush", jar, req });
    });
  },
};

Services.obs.addObserver(
  {
    observe() {
      // Belt and braces: Firefox says the last private context is gone.
      if (Object.values(engines).some(e => e.jars.private.started)) {
        CookieSync.endPrivate();
        for (const engine of Object.values(engines)) if (engine.proc) engine.write({ cmd: "private-end" });
      }
    },
  },
  "last-pb-context-exited"
);
