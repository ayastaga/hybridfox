/* Hybrid browser layer, loaded into every Firefox browser window (normal and
 * private) by hybrid.cfg.
 *
 * Each tab runs on one engine: Gecko (Firefox's own), Blink (Chromium) or
 * WebKit. A non-Gecko tab is an ordinary Firefox tab whose Gecko browser
 * holds a blank placeholder page while the engine's view covers its content
 * area. This script maps the tab strip, address bar, navigation, zoom,
 * titles, icons, dialogs, history, site rules and session restore onto the
 * engines, and runs the extension bridge: the placeholder page takes on the
 * identity of the site the engine shows, and re-asks each of the engine's
 * requests from there, so Firefox's request-filtering extensions see them.
 */
(function hybridBrowser(window) {
  "use strict";

  const { HybridHost, ENGINES } = ChromeUtils.importESModule("resource://hybrid/HybridHost.sys.mjs");
  const { PlacesUtils } = ChromeUtils.importESModule("resource://gre/modules/PlacesUtils.sys.mjs");
  const { PrivateBrowsingUtils } = ChromeUtils.importESModule("resource://gre/modules/PrivateBrowsingUtils.sys.mjs");
  const { document, gBrowser, gURLBar } = window;
  const env = Cc["@mozilla.org/process/environment;1"].getService(Ci.nsIEnvironment);
  const DEBUG = !!env.get("HYBRID_DEBUG");
  const log = (...a) => console.log("[hybrid]", ...a);
  const debug = (...a) => DEBUG && console.log("[hybrid]", ...a);
  const systemPrincipal = Services.scriptSecurityManager.getSystemPrincipal();

  const isPrivate = PrivateBrowsingUtils.isWindowPrivate(window);
  const JAR = isPrivate ? "private" : "normal";
  const SESSION_KEY = "hybrid-engine-tab";
  const RULES_PREF = "hybrid.siteRules";
  const ENGINE_ORDER = ["gecko", "blink", "webkit"];
  const LABEL = { gecko: "Gecko", blink: "Blink", webkit: "WebKit" };
  const MENU_LABEL = {
    gecko: "Firefox engine (Gecko)",
    blink: "Chromium engine (Blink)",
    webkit: "WebKit engine (Safari’s)",
  };

  let winId = null; // assigned when this window registers, at the end
  let overlayOpen = false;
  const engineFailures = {};
  const tabsById = new Map();
  const send = (engine, obj) => HybridHost.send(engine, obj);
  const usedEngines = new Set(); // engines this window has opened tabs with

  // -------------------------------------------------------- host events --

  function onHostEvent(ev) {
    const tab = tabsById.get(ev.id);
    const h = tab?._hybrid;
    switch (ev.event) {
      case "error":
        log("tab error", ev.id, ev.msg);
        break;
      case "title":
        if (!h) break;
        h.title = ev.title || h.url;
        setTabLabel(tab, h.title);
        break;
      case "url":
        if (!h) break;
        h.url = ev.url;
        persist(tab);
        mirrorIdentity(tab);
        refreshSoon(tab);
        break;
      case "favicon":
        if (h) setEngineIcon(tab, ev.url);
        break;
      case "blocked":
        if (!h) break;
        h.blocked++;
        debug("blocked", ev.reason, ev.url);
        if (tab.selected) updateChrome();
        break;
      case "unsafe":
        // Safe Browsing flagged the page. Show it in Gecko, where Firefox's
        // own warning page explains the danger and offers a way back.
        if (h) {
          log("Safe Browsing blocked a", ev.reason, "page; showing Firefox's warning");
          toGecko(tab, ev.url);
        }
        break;
      case "loading":
        if (!h) break;
        if (ev.loading && !h.loading) h.blocked = 0;
        if (!ev.loading) recordHistory(h);
        h.loading = ev.loading;
        h.canGoBack = ev.canGoBack;
        h.canGoForward = ev.canGoForward;
        tab.toggleAttribute("busy", ev.loading);
        if (tab.selected) updateChrome();
        break;
      case "popup": {
        // The engine already created the popup (it keeps window.opener,
        // which sign-in popups use to report back). Give it a tab.
        const opener = tabsById.get(ev.opener);
        const newTab = gBrowser.addTrustedTab("about:blank", {
          inBackground: !!ev.background,
          relatedToCurrent: true,
          ownerTab: opener,
        });
        adopt(newTab, ev.engine, ev.id, ev.url);
        break;
      }
      case "closed":
        // The page closed itself (window.close(), e.g. a finished sign-in
        // popup). Tabs Firefox closed are already unmapped.
        if (tab) {
          tabsById.delete(ev.id);
          delete tab._hybrid;
          gBrowser.removeTab(tab);
        }
        HybridHost.releaseTab(ev.id);
        break;
      case "dialog":
        showDialog(tab, ev);
        break;
      case "shortcut":
        handleShortcut(ev.key);
        break;
    }
  }

  function onEngineFail(engine, msg) {
    engineFailures[engine] = msg;
    for (const tab of tabsById.values()) {
      if (tab._hybrid.engine === engine) tab.setAttribute("hybrid-dead", "true");
    }
    updateChrome();
  }

  // ------------------------------------------------------- tab plumbing --

  function contentRect() {
    const r = gBrowser.selectedBrowser.getBoundingClientRect();
    const dpr = window.devicePixelRatio;
    return {
      x: Math.round(r.left * dpr),
      y: Math.round(r.top * dpr),
      w: Math.max(1, Math.round(r.width * dpr)),
      h: Math.max(1, Math.round(r.height * dpr)),
    };
  }

  // Engine switches replace the current history entry so Firefox's back
  // button never lands on the blank placeholder page.
  function geckoLoad(browser, spec) {
    const opts = {
      triggeringPrincipal: systemPrincipal,
      loadFlags: Ci.nsIWebNavigation.LOAD_FLAGS_REPLACE_HISTORY,
    };
    if (browser.fixupAndLoadURIString) browser.fixupAndLoadURIString(spec, opts);
    else browser.loadURI(Services.io.newURI(spec), opts);
  }

  function webURL(spec) {
    return /^(https?|file|data):/i.test(spec) ? spec : "about:blank";
  }

  function setTabLabel(tab, label) {
    try {
      gBrowser._setTabLabel(tab, label, { isContentTitle: true });
    } catch (e) {
      tab.setAttribute("label", label);
    }
  }

  // Firefox resets a tab's label and address bar when we cancel its own
  // loads; re-apply what the engine last reported once it settles.
  function refreshTab(tab) {
    const h = tab._hybrid;
    if (!h) return;
    if (h.title) setTabLabel(tab, h.title);
    if (tab.selected) updateChrome();
  }
  function refreshSoon(tab) {
    for (const ms of [0, 150, 500, 1200]) window.setTimeout(() => refreshTab(tab), ms);
  }

  // Firefox only accepts local icon URLs for tabs, so fetch the site's icon
  // and hand it over as a data: URL.
  const iconCache = new Map();
  async function setEngineIcon(tab, url) {
    try {
      let dataURL = iconCache.get(url);
      if (!dataURL) {
        if (url.startsWith("data:")) {
          dataURL = url;
        } else {
          const res = await window.fetch(url, { credentials: "omit" });
          if (!res.ok) return;
          const blob = await res.blob();
          if (blob.size > 512 * 1024 || !/^image\//.test(blob.type || "image/")) return;
          dataURL = await new Promise((resolve, reject) => {
            const r = new window.FileReader();
            r.onload = () => resolve(r.result);
            r.onerror = reject;
            r.readAsDataURL(blob);
          });
        }
        if (!isPrivate) iconCache.set(url, dataURL);
      }
      if (tab._hybrid) gBrowser.setIcon(tab, dataURL);
    } catch (e) {
      debug("icon failed", e);
    }
  }

  function newState(engine, id, url, title, visible) {
    return {
      engine, id, url, title, visible,
      loading: true, canGoBack: false, canGoForward: false,
      dialogs: 0, zoom: 1, blocked: 0,
    };
  }

  function markTab(tab) {
    const e = tab._hybrid.engine;
    tab.setAttribute("hybrid-engine", e);
    tab.setAttribute("tooltiptext", MENU_LABEL[e] + (isPrivate ? ", private" : ""));
  }

  function unmarkTab(tab) {
    for (const a of ["hybrid-engine", "hybrid-dead", "busy", "tooltiptext"]) tab.removeAttribute(a);
  }

  function engineLabel(h) {
    let label = LABEL[h.engine];
    if (h.zoom !== 1) label += ` ${Math.round(h.zoom * 100)}%`;
    if (h.blocked) label += ` · ${h.blocked} blocked`;
    return label;
  }

  // Visits in engine tabs go into Firefox's history (never in private
  // windows), so history and "forget about this site" cover every engine.
  function recordHistory(h) {
    if (isPrivate || !/^https?:/i.test(h.url) || h.url === h.historyURL) return;
    h.historyURL = h.url;
    PlacesUtils.history
      .insert({
        url: h.url,
        title: h.title && h.title !== h.url ? h.title : undefined,
        visits: [{ transition: PlacesUtils.history.TRANSITIONS.LINK, date: new Date() }],
      })
      .catch(e => debug("history insert failed", e));
  }

  // ---- per-site engine rules ("always open example.com in WebKit")
  function siteOf(spec) {
    try {
      const uri = Services.io.newURI(spec);
      if (!/^https?$/.test(uri.scheme)) return "";
      try {
        return Services.eTLD.getBaseDomain(uri);
      } catch (e) {
        return uri.host;
      }
    } catch (e) {
      return "";
    }
  }
  function getRules() {
    try {
      return JSON.parse(Services.prefs.getStringPref(RULES_PREF, "{}")) || {};
    } catch (e) {
      return {};
    }
  }
  function ruleFor(site) {
    const r = site ? getRules()[site] : null;
    return r === "chromium" ? "blink" : r && ENGINES[r] ? r : null; // older rules said "chromium"
  }
  function setRule(site, engine) {
    // Never record sites from private windows.
    if (!site || isPrivate) return;
    const rules = getRules();
    if (engine) rules[site] = engine;
    else delete rules[site];
    Services.prefs.setStringPref(RULES_PREF, JSON.stringify(rules));
  }

  // ------------------------------------------------- the placeholder page --
  // While an engine shows a page, the Gecko browser underneath holds a blank
  // document carrying that site's identity (and, where allowed, its
  // address). Requests made from it look to extensions like the site's own.

  const FRAME_SCRIPT = `
    const { NetUtil } = ChromeUtils.importESModule("resource://gre/modules/NetUtil.sys.mjs");
    const NOT_CACHED = 0x804b0046;
    addMessageListener("hybrid:address", ({ data }) => {
      try {
        content.history.replaceState(null, "", data.url);
        sendAsyncMessage("hybrid:address-done", { ok: true });
      } catch (e) {
        sendAsyncMessage("hybrid:address-done", { ok: false, error: String(e) });
      }
    });
    addMessageListener("hybrid:probe", ({ data }) => {
      const reply = r => sendAsyncMessage("hybrid:probe-done", { req: data.req, ...r });
      let ch;
      try {
        ch = NetUtil.newChannel({
          uri: data.url,
          loadingNode: content.document,
          securityFlags: Ci.nsILoadInfo.SEC_ALLOW_CROSS_ORIGIN_SEC_CONTEXT_IS_NULL,
          contentPolicyType: data.policyType,
        });
        // Decision only: extensions decide before the cache is consulted;
        // allowed requests then stop at "not cached" without any network.
        ch.loadFlags |= Ci.nsIRequest.LOAD_FROM_CACHE | Ci.nsICachingChannel.LOAD_ONLY_FROM_CACHE;
      } catch (e) {
        reply({ allow: true, note: "no channel: " + e });
        return;
      }
      let finalURL = "";
      let mime = "";
      let body = "";
      try {
        ch.asyncOpen({
          onStartRequest(r) {
            try {
              const c = r.QueryInterface(Ci.nsIChannel);
              finalURL = c.URI.spec;
              mime = c.contentType || "";
            } catch (e) {}
          },
          onDataAvailable(r, s, o, n) {
            const chunk = NetUtil.readInputStreamToString(s, n);
            if (body.length < 4 * 1024 * 1024) body += chunk;
          },
          onStopRequest(r, status) {
            if (finalURL.startsWith("moz-extension:")) reply({ redirectBody: body, redirectMime: mime });
            else if (status === Cr.NS_ERROR_ABORT) reply({ block: true });
            else if (finalURL && finalURL !== data.url && /^https?:/.test(finalURL)) reply({ redirectURL: finalURL });
            else reply({ allow: true });
          },
        });
      } catch (e) {
        reply({ allow: true, note: "open failed: " + e });
      }
    });`;

  // CEF resource types -> Firefox content policy types, so extension rules
  // that match on type ($script, $image, ...) apply.
  const POLICY_TYPE = {
    1: Ci.nsIContentPolicy.TYPE_SUBDOCUMENT,
    2: Ci.nsIContentPolicy.TYPE_STYLESHEET,
    3: Ci.nsIContentPolicy.TYPE_SCRIPT,
    4: Ci.nsIContentPolicy.TYPE_IMAGE,
    5: Ci.nsIContentPolicy.TYPE_FONT,
    7: Ci.nsIContentPolicy.TYPE_OBJECT,
    8: Ci.nsIContentPolicy.TYPE_MEDIA,
    9: Ci.nsIContentPolicy.TYPE_SCRIPT,
    10: Ci.nsIContentPolicy.TYPE_SCRIPT,
    12: Ci.nsIContentPolicy.TYPE_IMAGE,
    13: Ci.nsIContentPolicy.TYPE_XMLHTTPREQUEST,
    14: Ci.nsIContentPolicy.TYPE_PING,
    15: Ci.nsIContentPolicy.TYPE_SCRIPT,
    16: Ci.nsIContentPolicy.TYPE_CSP_REPORT,
  };

  const probeWaiters = new Map();
  let nextProbe = 1;

  function ensureFrameScript(browser) {
    if (browser._hybridFrameScript) return;
    browser._hybridFrameScript = true;
    const mm = browser.messageManager;
    mm.addMessageListener("hybrid:probe-done", m => {
      const done = probeWaiters.get(m.data.req);
      probeWaiters.delete(m.data.req);
      if (done) done(m.data);
    });
    mm.addMessageListener("hybrid:address-done", m => {
      if (!m.data.ok) debug("address mirroring unavailable:", m.data.error);
    });
    mm.loadFrameScript("data:application/javascript," + encodeURIComponent(FRAME_SCRIPT), true);
  }

  // Give the placeholder the identity of the page the engine shows.
  function mirrorIdentity(tab) {
    const h = tab._hybrid;
    if (!h || !/^https?:/.test(h.url)) return;
    let uri;
    try {
      uri = Services.io.newURI(h.url);
    } catch (e) {
      return;
    }
    const browser = tab.linkedBrowser;
    ensureFrameScript(browser);
    if (h.identityOrigin !== uri.prePath) {
      try {
        const principal = Services.scriptSecurityManager.createContentPrincipal(
          uri,
          isPrivate ? { privateBrowsingId: 1 } : {}
        );
        h.mirrorGuard = Date.now();
        browser.createAboutBlankDocumentViewer(principal, principal);
        h.identityOrigin = uri.prePath;
      } catch (e) {
        debug("could not set placeholder identity", e);
        return;
      }
    }
    // Show the real address to extensions too (same-site address change).
    h.mirroredURL = h.url;
    browser.messageManager.sendAsyncMessage("hybrid:address", { url: h.url });
  }

  // Asked by HybridHost for each engine request while request-filtering
  // extensions are installed.
  function probe(ev) {
    const tab = tabsById.get(ev.id);
    const h = tab?._hybrid;
    if (!h) return Promise.resolve({ allow: true });
    if (ev.top && ev.top !== h.url && /^https?:/.test(ev.top)) {
      try {
        if (Services.io.newURI(ev.top).prePath !== h.identityOrigin) {
          h.url = ev.top;
          mirrorIdentity(tab);
        }
      } catch (e) {}
    }
    if (!h.identityOrigin) mirrorIdentity(tab);
    return new Promise(resolve => {
      const req = nextProbe++;
      const timer = window.setTimeout(() => {
        probeWaiters.delete(req);
        resolve({ allow: true });
      }, 1000);
      probeWaiters.set(req, r => {
        window.clearTimeout(timer);
        if (r.redirectBody !== undefined) r.redirectBody = window.btoa(r.redirectBody);
        resolve(r);
      });
      tab.linkedBrowser.messageManager.sendAsyncMessage("hybrid:probe", {
        req,
        url: ev.url,
        policyType: POLICY_TYPE[ev.type] ?? Ci.nsIContentPolicy.TYPE_OTHER,
      });
    });
  }

  // ---------------------------------------------------- engine switching --

  // A popup the engine already created: give it this tab.
  function adopt(tab, engine, id, url) {
    usedEngines.add(engine);
    tab._hybrid = newState(engine, id, webURL(url || "about:blank"), url, false);
    tabsById.set(id, tab);
    markTab(tab);
    geckoLoad(tab.linkedBrowser, "about:blank");
    persist(tab);
    syncVisibility();
    if (tab.selected) {
      updateChrome();
      send(engine, { cmd: "focus", id });
    }
  }

  function openInEngine(tab, engine, url) {
    if (!url) url = tab.linkedBrowser.currentURI?.spec || "about:blank";
    url = webURL(url);
    HybridHost.use(engine, winId);
    usedEngines.add(engine);
    const id = HybridHost.allocTabId(winId, engine);
    const visible = tab.selected && !overlayOpen;
    tab._hybrid = newState(engine, id, url, tab.label, visible);
    tabsById.set(id, tab);
    markTab(tab);
    geckoLoad(tab.linkedBrowser, "about:blank");
    send(engine, { cmd: "create", id, win: winId, jar: JAR, url, visible, ...contentRect() });
    persist(tab);
    if (tab.selected) {
      updateChrome();
      send(engine, { cmd: "focus", id });
    }
  }

  // Leave the current engine, pulling its latest cookies into Firefox first.
  async function leaveEngine(tab) {
    const h = tab._hybrid;
    h.switching = true;
    await HybridHost.flushCookies(h.engine, JAR);
    if (tab._hybrid !== h) return null;
    send(h.engine, { cmd: "close", id: h.id });
    tabsById.delete(h.id);
    HybridHost.releaseTab(h.id);
    delete tab._hybrid;
    unmarkTab(tab);
    return h;
  }

  async function toGecko(tab, urlOverride) {
    const h = tab?._hybrid;
    if (!h || h.switching) return;
    if (!(await leaveEngine(tab))) return;
    tab.linkedBrowser.userTypedValue = null;
    try {
      SessionStore.deleteCustomTabValue(tab, SESSION_KEY);
    } catch (e) {}
    const target = urlOverride || h.url;
    // The user chose Gecko for this site in this tab: don't bounce it back
    // because of a site rule.
    tab._hybridManualGecko = siteOf(target);
    geckoLoad(tab.linkedBrowser, target === "about:blank" ? "about:newtab" : target);
    if (tab.selected) {
      updateChrome();
      tab.linkedBrowser.focus();
    }
  }

  async function switchEngine(tab, engine, url) {
    if (!tab) return;
    const current = tab._hybrid?.engine || "gecko";
    if (current === engine) return;
    if (engine === "gecko") return toGecko(tab, url);
    if (engineFailures[engine] || HybridHost.failureOf(engine)) {
      log(MENU_LABEL[engine], "unavailable:", engineFailures[engine] || HybridHost.failureOf(engine));
      return;
    }
    if (tab._hybrid) {
      if (tab._hybrid.switching) return;
      const h = await leaveEngine(tab);
      if (!h) return;
      url = url || h.url;
    }
    openInEngine(tab, engine, url);
  }

  function cycleEngine(tab) {
    const current = tab._hybrid?.engine || "gecko";
    const order = ENGINE_ORDER.filter(e => e === "gecko" || !HybridHost.failureOf(e));
    const next = order[(order.indexOf(current) + 1) % order.length];
    switchEngine(tab, next);
  }

  function persist(tab) {
    // Private windows are never saved by Firefox's session store anyway.
    if (isPrivate || !tab._hybrid) return;
    try {
      SessionStore.setCustomTabValue(tab, SESSION_KEY, JSON.stringify({ engine: tab._hybrid.engine, url: tab._hybrid.url }));
    } catch (e) {}
  }

  function restoreSession(tabs = gBrowser.tabs) {
    if (isPrivate) return;
    for (const tab of tabs) {
      if (tab._hybrid) continue;
      let saved = null;
      try {
        saved = SessionStore.getCustomTabValue(tab, SESSION_KEY);
        if (!saved) {
          const old = SessionStore.getCustomTabValue(tab, "hybrid-chromium-url"); // earlier versions
          if (old) saved = JSON.stringify({ engine: "blink", url: old });
        }
      } catch (e) {}
      if (!saved) continue;
      try {
        const { engine, url } = JSON.parse(saved);
        if (ENGINES[engine]) switchEngine(tab, engine, url);
      } catch (e) {}
    }
  }

  // Show only the selected tab's engine view; hide the rest.
  function syncVisibility() {
    const selected = gBrowser.selectedTab;
    for (const tab of tabsById.values()) {
      const h = tab._hybrid;
      const want = tab === selected && !overlayOpen && !(h.dialogs > 0);
      if (want && !h.visible) {
        h.visible = true;
        h.rect = contentRect();
        send(h.engine, { cmd: "show", id: h.id, ...h.rect });
      } else if (!want && h.visible) {
        h.visible = false;
        send(h.engine, { cmd: "hide", id: h.id });
      }
    }
  }

  function syncGeometry() {
    const h = gBrowser.selectedTab._hybrid;
    if (!h || !h.visible) return;
    const r = contentRect();
    if (h.rect && r.x === h.rect.x && r.y === h.rect.y && r.w === h.rect.w && r.h === h.rect.h) return;
    h.rect = r;
    send(h.engine, { cmd: "bounds", id: h.id, ...r });
  }

  // ---------------------------------------------------------------- zoom --
  // Firefox's zoom steps, applied in the engine instead of the placeholder.
  // Engines take a Chromium-style level: log base 1.2 of the factor.
  const ZOOM_STEPS = (() => {
    try {
      return Services.prefs.getCharPref("toolkit.zoomManager.zoomValues").split(",").map(Number)
        .filter(n => n > 0).sort((a, b) => a - b);
    } catch (e) {
      return [0.3, 0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.2, 1.33, 1.5, 1.7, 2, 2.4, 3, 4, 5];
    }
  })();

  function zoomEngine(h, direction) {
    let next = 1;
    if (direction > 0) next = ZOOM_STEPS.find(z => z > h.zoom + 1e-6) ?? h.zoom;
    else if (direction < 0) next = [...ZOOM_STEPS].reverse().find(z => z < h.zoom - 1e-6) ?? h.zoom;
    h.zoom = next;
    send(h.engine, { cmd: "zoom", id: h.id, level: Math.log(next) / Math.log(1.2) });
    updateChrome();
  }

  const FZ = window.FullZoom;
  if (FZ) {
    for (const [name, dir] of [["enlarge", 1], ["reduce", -1], ["reset", 0]]) {
      const orig = FZ[name];
      if (typeof orig !== "function") continue;
      FZ[name] = function (...args) {
        const h = gBrowser.selectedTab._hybrid;
        if (h) return zoomEngine(h, dir);
        return orig.apply(this, args);
      };
    }
  }

  // ------------------------------------------------------- browser chrome --

  function setDisabled(id, disabled) {
    const el = document.getElementById(id);
    if (!el) return;
    if (disabled) el.setAttribute("disabled", "true");
    else el.removeAttribute("disabled");
  }

  function updateChrome() {
    const tab = gBrowser.selectedTab;
    const h = tab._hybrid;
    engineButton.setAttribute("engine", h ? h.engine : "gecko");
    engineButton.setAttribute("label", h ? engineLabel(h) : "Gecko");
    engineButton.toggleAttribute("failed", !!(h && engineFailures[h.engine]));
    engineButton.setAttribute(
      "tooltiptext",
      h
        ? `This tab runs on the ${MENU_LABEL[h.engine]}, with Firefox’s tracking protection and Safe Browsing` +
            (h.blocked ? ` (${h.blocked} blocked on this page)` : "") + ". Click to choose an engine (Ctrl+Alt+E cycles)."
        : "This tab runs on Firefox’s engine. Click to choose an engine (Ctrl+Alt+E cycles)."
    );
    if (!h) return;
    tab.linkedBrowser.userTypedValue = h.url === "about:blank" ? "" : h.url;
    if (!gURLBar.focused) gURLBar.value = tab.linkedBrowser.userTypedValue;
    // Firefox recomputes these from the placeholder; overwrite after it.
    const apply = () => {
      if (gBrowser.selectedTab !== tab || !tab._hybrid) return;
      setDisabled("Browser:Back", !h.canGoBack);
      setDisabled("Browser:BackOrBackDuplicate", !h.canGoBack);
      setDisabled("Browser:Forward", !h.canGoForward);
      setDisabled("Browser:ForwardOrForwardDuplicate", !h.canGoForward);
      setDisabled("Browser:Reload", false);
      setDisabled("Browser:ReloadOrDuplicate", false);
      setDisabled("Browser:ReloadSkipCache", false);
    };
    apply();
    window.setTimeout(apply, 0);
    window.setTimeout(apply, 100);
  }

  // Route Firefox's navigation commands to the engine for engine tabs.
  function wrapCommand(obj, name, cmd, extra = {}) {
    const orig = obj?.[name];
    if (typeof orig !== "function") return;
    obj[name] = function (...args) {
      const h = gBrowser.selectedTab._hybrid;
      if (h) {
        send(h.engine, { cmd, id: h.id, ...extra });
        return undefined;
      }
      return orig.apply(this, args);
    };
  }
  const BC = window.BrowserCommands;
  if (BC) {
    wrapCommand(BC, "back", "back");
    wrapCommand(BC, "forward", "forward");
    wrapCommand(BC, "reload", "reload");
    wrapCommand(BC, "reloadOrDuplicate", "reload");
    wrapCommand(BC, "reloadSkipCache", "reload", { skipCache: true });
    wrapCommand(BC, "reloadWithFlags", "reload");
    wrapCommand(BC, "stop", "stop");
  } else {
    wrapCommand(window, "BrowserBack", "back");
    wrapCommand(window, "BrowserForward", "forward");
    wrapCommand(window, "BrowserReload", "reload");
    wrapCommand(window, "BrowserReloadOrDuplicate", "reload");
    wrapCommand(window, "BrowserReloadSkipCache", "reload", { skipCache: true });
    wrapCommand(window, "BrowserStop", "stop");
  }

  // Loads aimed at an engine tab's placeholder (address bar, bookmarks,
  // history, search) go to the engine instead; site rules send Gecko tabs to
  // their engine.
  const STATE_START = Ci.nsIWebProgressListener.STATE_START;
  const STATE_STOP = Ci.nsIWebProgressListener.STATE_STOP;
  const STATE_IS_DOCUMENT = Ci.nsIWebProgressListener.STATE_IS_DOCUMENT;
  const STATE_IS_WINDOW = Ci.nsIWebProgressListener.STATE_IS_WINDOW;
  const SAME_DOCUMENT = Ci.nsIWebProgressListener.LOCATION_CHANGE_SAME_DOCUMENT;

  function redirect(tab, spec) {
    const h = tab._hybrid;
    const now = Date.now();
    if (h.lastRedirect === spec && now - h.lastRedirectAt < 2000) return;
    h.lastRedirect = spec;
    h.lastRedirectAt = now;
    h.url = webURL(spec);
    send(h.engine, { cmd: "navigate", id: h.id, url: h.url });
    refreshSoon(tab);
  }

  gBrowser.addTabsProgressListener({
    onStateChange(browser, webProgress, request, flags) {
      if (!webProgress?.isTopLevel) return;
      const tab = gBrowser.getTabForBrowser(browser);
      if (!tab) return;
      if ((flags & STATE_STOP) && (flags & STATE_IS_WINDOW) && !tab._hybrid) {
        // Give script-rendered pages a moment to show a refusal.
        window.setTimeout(() => checkRefusal(browser), 1500);
      }
      if (!(flags & STATE_START) || !(flags & STATE_IS_DOCUMENT)) return;
      let spec = "";
      try {
        spec = request.QueryInterface(Ci.nsIChannel).originalURI.spec;
      } catch (e) {}
      if (!tab._hybrid) {
        // Gecko tab: a site rule may send this page to an engine.
        const site = siteOf(spec);
        if (site && tab._hybridManualGecko && tab._hybridManualGecko !== site) tab._hybridManualGecko = null;
        const engine = ruleFor(site);
        if (engine && tab._hybridManualGecko !== site && !HybridHost.failureOf(engine)) {
          try {
            request.cancel(Cr.NS_BINDING_ABORTED);
          } catch (e) {}
          window.setTimeout(() => switchEngine(tab, engine, spec), 0);
        }
        return;
      }
      if (!spec || spec === "about:blank") return;
      try {
        request.cancel(Cr.NS_BINDING_ABORTED);
      } catch (e) {}
      redirect(tab, spec);
    },
    onLocationChange(browser, webProgress, request, location, flags) {
      if (!webProgress?.isTopLevel) return;
      const tab = gBrowser.getTabForBrowser(browser);
      const h = tab?._hybrid;
      if (!h || location.spec === "about:blank") return;
      // Our own address mirroring of the placeholder: not a navigation.
      if ((flags & SAME_DOCUMENT) && location.spec === h.mirroredURL) return;
      if (location.spec === h.mirroredURL && Date.now() - (h.mirrorGuard || 0) < 3000) return;
      redirect(tab, location.spec);
      h.identityOrigin = null;
      geckoLoad(browser, "about:blank");
    },
  });

  // ---------------------------------------------------------- JS dialogs --
  // alert/confirm/prompt and "leave page?" from engine pages, shown with
  // Firefox's own tab prompts. The engine view hides while one is open.
  async function showDialog(tab, ev) {
    const h = tab?._hybrid;
    const reply = (ok, text = "") =>
      send(ev.engine, { cmd: "dialog-reply", req: ev.req, ok: !!ok, text: String(text ?? "") });
    if (!h) {
      reply(false);
      return;
    }
    if (!tab.selected) gBrowser.selectedTab = tab;
    h.dialogs++;
    syncVisibility();
    let title = "";
    try {
      title = new URL(ev.origin).host;
    } catch (e) {}
    const bc = tab.linkedBrowser.browsingContext;
    const TAB = Ci.nsIPrompt.MODAL_TYPE_TAB;
    let ok = false;
    let text = "";
    try {
      if (ev.type === "alert") {
        await Services.prompt.asyncAlert(bc, TAB, title, ev.message);
        ok = true;
      } else if (ev.type === "confirm") {
        ok = (await Services.prompt.asyncConfirm(bc, TAB, title, ev.message)).getProperty("ok");
      } else if (ev.type === "prompt") {
        const r = await Services.prompt.asyncPrompt(bc, TAB, title, ev.message, ev.defaultText ?? "", null, false);
        ok = r.getProperty("ok");
        text = r.getProperty("value");
      } else if (ev.type === "beforeunload") {
        ok = (await Services.prompt.asyncConfirm(
          bc, TAB, title,
          "This page is asking you to confirm that you want to leave. Information you’ve entered may not be saved."
        )).getProperty("ok");
      }
    } catch (e) {
      log("dialog failed", e);
    }
    h.dialogs--;
    syncVisibility();
    reply(ok, text);
  }

  // ------------------------------------------------------------------ UI --

  const css = `
    .tabbrowser-tab[hybrid-engine="blink"] .tab-background { box-shadow: inset 0 -3px 0 #1f9ec4 !important; }
    .tabbrowser-tab[hybrid-engine="webkit"] .tab-background { box-shadow: inset 0 -3px 0 #8e44ad !important; }
    .tabbrowser-tab[hybrid-dead] .tab-label { text-decoration: line-through; }
    #hybrid-engine-button .toolbarbutton-icon { display: none !important; }
    #hybrid-engine-button .toolbarbutton-text {
      display: flex !important; min-width: 4.5em; justify-content: center;
      padding-inline: 8px !important; font-weight: 600; border-radius: 6px;
    }
    #hybrid-engine-button[engine="gecko"] .toolbarbutton-text { color: #e8590c; }
    #hybrid-engine-button[engine="blink"] .toolbarbutton-text { color: #fff; background: #1f9ec4; }
    #hybrid-engine-button[engine="webkit"] .toolbarbutton-text { color: #fff; background: #8e44ad; }
    #hybrid-engine-button[failed] .toolbarbutton-text { text-decoration: line-through; }
  `;
  window.windowUtils.loadSheetUsingURIString("data:text/css," + encodeURIComponent(css), window.windowUtils.AUTHOR_SHEET);

  // An engine chooser menu: Gecko / Blink / WebKit for the given tab.
  function fillEngineMenu(popup, getTab) {
    popup.addEventListener("popupshowing", e => {
      if (e.target !== popup) return;
      const tab = getTab();
      const current = tab._hybrid?.engine || "gecko";
      for (const item of popup.children) {
        const engine = item.getAttribute("engine");
        item.toggleAttribute("checked", engine === current);
        item.toggleAttribute("disabled", engine !== "gecko" && !!(engineFailures[engine] || HybridHost.failureOf(engine)));
      }
    });
    for (const engine of ENGINE_ORDER) {
      const item = document.createXULElement("menuitem");
      item.setAttribute("type", "radio");
      item.setAttribute("engine", engine);
      item.setAttribute("label", MENU_LABEL[engine]);
      item.addEventListener("command", () => switchEngine(getTab(), engine));
      popup.append(item);
    }
  }

  const engineButton = document.createXULElement("toolbarbutton");
  engineButton.id = "hybrid-engine-button";
  engineButton.className = "toolbarbutton-1 chromeclass-toolbar-additional";
  engineButton.setAttribute("type", "menu");
  engineButton.setAttribute("label", "Gecko");
  engineButton.setAttribute("engine", "gecko");
  const buttonMenu = document.createXULElement("menupopup");
  buttonMenu.id = "hybrid-engine-menu";
  fillEngineMenu(buttonMenu, () => gBrowser.selectedTab);
  engineButton.append(buttonMenu);
  (document.getElementById("urlbar-container") || gURLBar.textbox).after(engineButton);

  // Tab context menu: engine submenu and site rule.
  const tabMenu = document.getElementById("tabContextMenu");
  const contextTab = () => window.TabContextMenu?.contextTab || gBrowser.selectedTab;
  if (tabMenu) {
    const menu = document.createXULElement("menu");
    menu.id = "hybrid-tab-engine";
    menu.setAttribute("label", "Tab Engine");
    const popup = document.createXULElement("menupopup");
    fillEngineMenu(popup, contextTab);
    menu.append(popup);
    const ruleItem = document.createXULElement("menuitem");
    ruleItem.id = "hybrid-site-rule";
    ruleItem.setAttribute("type", "checkbox");
    const sep = document.createXULElement("menuseparator");
    tabMenu.prepend(menu, ruleItem, sep);

    const tabSite = tab => siteOf(tab._hybrid ? tab._hybrid.url : tab.linkedBrowser.currentURI?.spec || "");
    // The rule offered: the tab's own engine, or Chromium's for Gecko tabs.
    const ruleEngine = tab => tab._hybrid?.engine || "blink";
    tabMenu.addEventListener("popupshowing", e => {
      if (e.target !== tabMenu) return;
      const tab = contextTab();
      const site = tabSite(tab);
      const engine = ruleEngine(tab);
      ruleItem.hidden = !site || isPrivate || !!HybridHost.failureOf(engine);
      ruleItem.setAttribute("label", `Always Open ${site} with ${MENU_LABEL[engine]}`);
      ruleItem.toggleAttribute("checked", ruleFor(site) === engine);
    });
    ruleItem.addEventListener("command", () => {
      const tab = contextTab();
      const site = tabSite(tab);
      const engine = ruleEngine(tab);
      const on = ruleFor(site) !== engine;
      setRule(site, on ? engine : null);
      if (on && !tab._hybrid) switchEngine(tab, engine);
      log(on ? "rule added:" : "rule removed:", site, on ? engine : "");
    });
  }

  // Page context menu: open a link with another engine.
  const pageMenu = document.getElementById("contentAreaContextMenu");
  if (pageMenu) {
    const anchor = document.getElementById("context-openlinkintab");
    const items = ["blink", "webkit"].map(engine => {
      const item = document.createXULElement("menuitem");
      item.id = `hybrid-open-link-${engine}`;
      item.setAttribute("label", `Open Link in New ${ENGINES[engine].label} Tab`);
      item.addEventListener("command", () => {
        const url = window.gContextMenu?.linkURL;
        if (!url) return;
        const tab = gBrowser.addTrustedTab("about:blank", { inBackground: true, relatedToCurrent: true });
        switchEngine(tab, engine, url);
      });
      return item;
    });
    anchor ? anchor.after(...items) : pageMenu.prepend(...items);
    pageMenu.addEventListener("popupshowing", e => {
      if (e.target !== pageMenu) return;
      for (const item of items) {
        const engine = item.id.replace("hybrid-open-link-", "");
        item.hidden = !window.gContextMenu?.onLink || !!HybridHost.failureOf(engine);
      }
    });
  }

  function focusAddressBar(engine) {
    send(engine, { cmd: "unfocus", win: winId });
    window.setTimeout(() => {
      gURLBar.focus();
      gURLBar.select();
    }, 0);
  }

  function handleShortcut(key) {
    const h = gBrowser.selectedTab._hybrid;
    const engine = h?.engine || "blink";
    const unfocus = () => send(engine, { cmd: "unfocus", win: winId });
    switch (key) {
      case "ctrl+t":
        unfocus();
        BC?.openTab ? BC.openTab() : window.BrowserOpenTab?.();
        break;
      case "ctrl+shift+t":
        unfocus();
        if (window.undoCloseTab) window.undoCloseTab();
        else window.SessionStore?.undoCloseTab(window, 0);
        break;
      case "ctrl+w":
        gBrowser.removeTab(gBrowser.selectedTab, { animate: true });
        break;
      case "ctrl+n":
        unfocus();
        window.OpenBrowserWindow();
        break;
      case "ctrl+shift+p":
        unfocus();
        window.OpenBrowserWindow({ private: true });
        break;
      case "ctrl+l":
      case "f6":
        focusAddressBar(engine);
        break;
      case "ctrl+tab":
      case "ctrl+pagedown":
        gBrowser.tabContainer.advanceSelectedTab(1, true);
        break;
      case "ctrl+shift+tab":
      case "ctrl+pageup":
        gBrowser.tabContainer.advanceSelectedTab(-1, true);
        break;
      case "f5":
      case "ctrl+r":
        if (h) send(h.engine, { cmd: "reload", id: h.id });
        break;
      case "shift+f5":
      case "ctrl+shift+r":
        if (h) send(h.engine, { cmd: "reload", id: h.id, skipCache: true });
        break;
      case "zoom-in":
        if (h) zoomEngine(h, 1);
        break;
      case "zoom-out":
        if (h) zoomEngine(h, -1);
        break;
      case "zoom-reset":
        if (h) zoomEngine(h, 0);
        break;
      case "ctrl+alt+e":
        cycleEngine(gBrowser.selectedTab);
        break;
    }
  }

  window.addEventListener(
    "keydown",
    e => {
      if (e.ctrlKey && e.altKey && !e.shiftKey && e.code === "KeyE") {
        e.preventDefault();
        e.stopPropagation();
        cycleEngine(gBrowser.selectedTab);
      }
    },
    true
  );

  // ---- "this site refuses Firefox" detection
  // After a Gecko page loads, read its visible text with Firefox's own
  // PageExtractor and look for the usual refusals. Nothing leaves the browser.
  const REFUSALS = [
    /\b(your|this) browser (is|isn['’]t|is not) (not )?(fully )?supported\b/i,
    /\bunsupported browser\b/i,
    /\bbrowser (is )?not supported\b/i,
    /\b(please )?(use|switch to|download|open (this|it) in) (the latest (version of )?)?(google )?chrome\b/i,
    /\bworks best (in|with|on) (google )?chrome\b/i,
    /\bchrome (or|and) (microsoft )?edge (are|is) (required|supported)\b/i,
  ];

  async function checkRefusal(browser) {
    const tab = gBrowser.getTabForBrowser(browser);
    if (!tab || tab._hybrid || HybridHost.failureOf("blink")) return;
    if (!/^https?:|^file:/.test(browser.currentURI?.spec || "")) return;
    let text = "";
    try {
      const actor = browser.browsingContext?.currentWindowGlobal?.getActor("PageExtractor");
      const result = await actor?.getText({});
      text = (result?.text || "").slice(0, 20000);
    } catch (e) {
      debug("page text unavailable", e);
      return;
    }
    const m = REFUSALS.map(re => text.match(re)).find(Boolean);
    if (m) suggestEngine(browser, m[0]);
  }

  async function suggestEngine(browser, phrase) {
    const tab = gBrowser.getTabForBrowser(browser);
    if (!tab || tab._hybrid) return;
    const site = siteOf(browser.currentURI?.spec || "");
    if (!site || tab._hybridManualGecko === site) return;
    const box = gBrowser.getNotificationBox(browser);
    if (box.getNotificationWithValue("hybrid-compat")) return;
    log("site may not support Gecko:", site, "- page says:", phrase);
    const buttons = [{ label: "Open in Chromium", accessKey: "O", callback: () => switchEngine(tab, "blink") }];
    if (!isPrivate) {
      buttons.push({
        label: `Always for ${site}`,
        accessKey: "A",
        callback: () => {
          setRule(site, "blink");
          switchEngine(tab, "blink");
        },
      });
    }
    try {
      await box.appendNotification(
        "hybrid-compat",
        {
          label: `${site} may not support Firefox’s engine. Open it with Chromium’s engine instead?`,
          priority: box.PRIORITY_INFO_HIGH,
        },
        buttons
      );
    } catch (e) {
      log("could not show the switch suggestion", e);
    }
  }

  // Another window adopting one of our engine tabs (tab dragged out, or
  // "Move to New Window") reopens it with the same engine there.
  window.hybridAdoptEngineTab = (tab, engine, url) => switchEngine(tab, engine, url);

  // ------------------------------------------------------------- events --

  gBrowser.tabContainer.addEventListener("TabSelect", () => {
    syncVisibility();
    const h = gBrowser.selectedTab._hybrid;
    for (const engine of usedEngines) {
      if (h?.engine === engine) send(engine, { cmd: "focus", id: h.id });
      else send(engine, { cmd: "unfocus", win: winId });
    }
    updateChrome();
  });

  gBrowser.tabContainer.addEventListener("TabClose", e => {
    const tab = e.target;
    const h = tab._hybrid;
    if (!h) return;
    send(h.engine, { cmd: "close", id: h.id });
    tabsById.delete(h.id);
    HybridHost.releaseTab(h.id);
    const adopter = e.detail?.adoptedBy;
    if (adopter) {
      const { engine, url } = h;
      window.setTimeout(() => adopter.ownerGlobal?.hybridAdoptEngineTab?.(adopter, engine, url), 0);
    }
  });

  gBrowser.tabContainer.addEventListener("SSTabRestored", e => restoreSession([e.target]));

  // Firefox UI that draws inside the window over the content area (the
  // address bar's results) would be hidden behind an engine view.
  const urlbarEl = gURLBar.textbox || document.getElementById("urlbar");
  new window.MutationObserver(() => {
    const open = urlbarEl.hasAttribute("open");
    if (open !== overlayOpen) {
      overlayOpen = open;
      syncVisibility();
    }
  }).observe(urlbarEl, { attributes: true, attributeFilter: ["open"] });

  window.addEventListener("resize", syncGeometry);
  window.setInterval(syncGeometry, 150);
  window.addEventListener("unload", () => HybridHost.unregisterWindow(winId));

  winId = HybridHost.registerWindow(
    window,
    { onEvent: onHostEvent, onEngineFail, probe },
    isPrivate
  );
  updateChrome();
  restoreSession();
  log("hybrid layer loaded", isPrivate ? "(private window)" : "");
})(window);
