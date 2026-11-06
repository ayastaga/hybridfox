// chromium_host: the Chromium engine of HybridFox.
//
// Firefox (the shell) spawns one of these per browser session and talks to
// it with one JSON object per line on stdin. Events go back on stdout,
// prefixed "@@" so Chromium's own logging can't be mistaken for them.
//
// Each Chromium "tab" is a CEF browser in an X11 child window placed over
// the content area of a Firefox window. Several Firefox windows can attach.
// Tabs in private windows use a separate in-memory profile ("jar" private)
// that is thrown away when the private session ends; normal tabs use the
// on-disk profile ("jar" normal). Each jar's cookies are mirrored with the
// matching Firefox cookie jar.

#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <dirent.h>
#include <ftw.h>
#include <signal.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "include/base/cef_callback.h"
#include "include/cef_app.h"
#include "include/cef_browser.h"
#include "include/cef_client.h"
#include "include/cef_command_line.h"
#include "include/cef_cookie.h"
#include "include/cef_jsdialog_handler.h"
#include "include/cef_parser.h"
#include "include/cef_request_context.h"
#include "include/cef_request_handler.h"
#include "include/cef_resource_request_handler.h"
#include "include/cef_parser.h"
#include "include/cef_stream.h"
#include "include/wrapper/cef_stream_resource_handler.h"
#include "include/wrapper/cef_closure_task.h"
#include "include/wrapper/cef_helpers.h"

namespace {

// ---------------------------------------------------------------- output --

std::mutex g_out_mutex;

void Emit(CefRefPtr<CefDictionaryValue> dict) {
  CefRefPtr<CefValue> v = CefValue::Create();
  v->SetDictionary(dict);
  std::string json = CefWriteJSON(v, JSON_WRITER_DEFAULT).ToString();
  std::lock_guard<std::mutex> lock(g_out_mutex);
  std::fprintf(stdout, "@@%s\n", json.c_str());
  std::fflush(stdout);
}

CefRefPtr<CefDictionaryValue> Event(const char* name, int id = -1) {
  auto d = CefDictionaryValue::Create();
  d->SetString("event", name);
  if (id >= 0) d->SetInt("id", id);
  return d;
}

void Log(const std::string& msg) {
  auto d = Event("log");
  d->SetString("msg", msg);
  Emit(d);
}

double NowSecs() {
  using namespace std::chrono;
  return duration<double>(system_clock::now().time_since_epoch()).count();
}

bool g_quitting = false;

// ------------------------------------------------------------------ X11 --

Display* g_dpy = nullptr;  // our own connection, separate from Chromium's

// One attached Firefox window.
struct Win {
  Window parent = 0;        // Firefox toplevel (client window)
  Window focus_target = 0;  // GTK's input-only focus proxy inside it
  int off_x = 0, off_y = 0; // content origin inside parent
};
std::map<int, Win> g_wins;

std::string GetName(Window w) {
  // Prefer the UTF-8 _NET_WM_NAME, fall back to WM_NAME.
  Atom net = XInternAtom(g_dpy, "_NET_WM_NAME", True);
  Atom utf8 = XInternAtom(g_dpy, "UTF8_STRING", True);
  std::string out;
  if (net != None && utf8 != None) {
    Atom type;
    int format;
    unsigned long n, after;
    unsigned char* data = nullptr;
    if (XGetWindowProperty(g_dpy, w, net, 0, 1024, False, utf8, &type, &format,
                           &n, &after, &data) == Success && data) {
      out.assign(reinterpret_cast<char*>(data), n);
    }
    if (data) XFree(data);
  }
  if (out.empty()) {
    char* name = nullptr;
    if (XFetchName(g_dpy, w, &name) && name) out = name;
    if (name) XFree(name);
  }
  return out;
}

// Depth-first search for a window whose title contains |marker|. The window
// manager reparents clients into frames, so look a few levels down.
Window FindByTitle(Window w, const std::string& marker, int depth) {
  if (depth > 5) return 0;
  if (depth > 0) {
    std::string name = GetName(w);
    if (!name.empty() && name.find(marker) != std::string::npos) return w;
  }
  Window root, parent, *children = nullptr;
  unsigned int n = 0;
  Window found = 0;
  if (XQueryTree(g_dpy, w, &root, &parent, &children, &n)) {
    for (unsigned int i = 0; i < n && !found; i++)
      found = FindByTitle(children[i], marker, depth + 1);
    if (children) XFree(children);
  }
  return found;
}

// Where Firefox's content (0,0) lies inside its toplevel, and GTK's focus
// proxy (keys sent to the toplevel itself are ignored by GTK).
void ComputeWinLayout(Win& win) {
  win.off_x = win.off_y = 0;
  win.focus_target = 0;
  Window root, parent, *children = nullptr;
  unsigned int n = 0;
  long best_area = 0;
  if (XQueryTree(g_dpy, win.parent, &root, &parent, &children, &n)) {
    for (unsigned int i = 0; i < n; i++) {
      XWindowAttributes a;
      if (!XGetWindowAttributes(g_dpy, children[i], &a)) continue;
      if (a.c_class == InputOnly && a.width == 1 && a.height == 1) {
        if (!win.focus_target) win.focus_target = children[i];
        continue;
      }
      if (a.c_class == InputOutput && a.map_state == IsViewable &&
          !a.override_redirect && (long)a.width * a.height > best_area) {
        best_area = (long)a.width * a.height;
        win.off_x = a.x;
        win.off_y = a.y;
      }
    }
    if (children) XFree(children);
  }
  if (const char* e = std::getenv("HYBRID_OFFSET_X")) win.off_x += std::atoi(e);
  if (const char* e = std::getenv("HYBRID_OFFSET_Y")) win.off_y += std::atoi(e);
}

// The child of the root window that contains |w| (its window-manager frame,
// or |w| itself when there is no window manager).
Window TopAncestor(Window w) {
  Window cur = w;
  for (int i = 0; i < 8; i++) {
    Window root, parent, *children = nullptr;
    unsigned int n = 0;
    if (!XQueryTree(g_dpy, cur, &root, &parent, &children, &n)) return 0;
    if (children) XFree(children);
    if (parent == root || parent == 0) return cur;
    cur = parent;
  }
  return cur;
}

// ------------------------------------------------------------------ jars --
// A jar is a Chromium profile (request context) plus the cookie mirroring
// with its Firefox counterpart. "normal" uses the on-disk global profile;
// "private" is an in-memory profile, replaced at every new private session.

struct CookieRec {
  std::string domain, path, name, value;
  bool secure = false, httponly = false, session = true;
  double expires = 0;  // seconds since the Unix epoch
  int same_site = CEF_COOKIE_SAME_SITE_UNSPECIFIED;
};

std::string CookieKey(const CookieRec& r) {
  return r.domain + "\t" + r.path + "\t" + r.name;
}

double ToEpoch(const cef_basetime_t& bt) {
  cef_time_t t = {};
  double d = 0;
  if (cef_time_from_basetime(bt, &t)) cef_time_to_doublet(&t, &d);
  return d;
}

cef_basetime_t FromEpoch(double d) {
  cef_time_t t = {};
  cef_basetime_t bt = {};
  cef_time_from_doublet(d, &t);
  cef_time_to_basetime(&t, &bt);
  return bt;
}

const char* SameSiteName(int v) {
  switch (v) {
    case CEF_COOKIE_SAME_SITE_NO_RESTRICTION: return "none";
    case CEF_COOKIE_SAME_SITE_LAX_MODE: return "lax";
    case CEF_COOKIE_SAME_SITE_STRICT_MODE: return "strict";
    default: return "unset";
  }
}

int SameSiteFrom(const std::string& s) {
  if (s == "none") return CEF_COOKIE_SAME_SITE_NO_RESTRICTION;
  if (s == "lax") return CEF_COOKIE_SAME_SITE_LAX_MODE;
  if (s == "strict") return CEF_COOKIE_SAME_SITE_STRICT_MODE;
  return CEF_COOKIE_SAME_SITE_UNSPECIFIED;
}

std::string CookieUrl(const CookieRec& c) {
  std::string host = c.domain;
  if (!host.empty() && host[0] == '.') host = host.substr(1);
  return std::string(c.secure ? "https://" : "http://") + host +
         (c.path.empty() ? "/" : c.path);
}

// Equal for mirroring purposes. SameSite counts: a site that tightens a
// cookie to Strict (or loosens it) without changing its value must reach the
// other engines too.
bool SameCookie(const CookieRec& a, const CookieRec& b) {
  return a.value == b.value && a.secure == b.secure &&
         a.httponly == b.httponly && a.session == b.session &&
         a.same_site == b.same_site &&
         (a.session || std::fabs(a.expires - b.expires) < 120);
}

CookieRec ReadCookieRec(CefRefPtr<CefDictionaryValue> c) {
  CookieRec r;
  r.domain = c->GetString("domain");
  r.path = c->GetString("path");
  r.name = c->GetString("name");
  r.value = c->GetString("value");
  r.secure = c->GetBool("secure");
  r.httponly = c->GetBool("httpOnly");
  r.session = c->GetBool("session");
  r.expires = c->GetDouble("expires");
  r.same_site = SameSiteFrom(c->GetString("sameSite"));
  return r;
}

class SetDone : public CefSetCookieCallback {
 public:
  explicit SetDone(std::function<void()> f) : f_(std::move(f)) {}
  void OnComplete(bool) override { if (f_) f_(); }
 private:
  std::function<void()> f_;
  IMPLEMENT_REFCOUNTING(SetDone);
};

class DeleteDone : public CefDeleteCookiesCallback {
 public:
  explicit DeleteDone(std::function<void()> f) : f_(std::move(f)) {}
  void OnComplete(int) override { if (f_) f_(); }
 private:
  std::function<void()> f_;
  IMPLEMENT_REFCOUNTING(DeleteDone);
};

struct Jar;
void FinishJarPoll(Jar* jar, std::map<std::string, CookieRec> current);

class CookieCollector : public CefCookieVisitor {
 public:
  explicit CookieCollector(Jar* jar) : jar_(jar) {}
  bool Visit(const CefCookie& c, int, int, bool&) override {
    CookieRec r;
    r.domain = CefString(&c.domain).ToString();
    r.path = CefString(&c.path).ToString();
    r.name = CefString(&c.name).ToString();
    r.value = CefString(&c.value).ToString();
    r.secure = c.secure;
    r.httponly = c.httponly;
    r.session = !c.has_expires;
    r.expires = c.has_expires ? ToEpoch(c.expires) : 0;
    r.same_site = c.same_site;
    cookies_[CookieKey(r)] = r;
    return true;
  }
  ~CookieCollector() override {
    // Released when the visit completes, including when there were none.
    CefPostTask(TID_UI, base::BindOnce(&FinishJarPoll, jar_, std::move(cookies_)));
  }
 private:
  Jar* jar_;
  std::map<std::string, CookieRec> cookies_;
  IMPLEMENT_REFCOUNTING(CookieCollector);
};

// Debugging and tests: reads a jar's cookies straight from Chromium's store
// (not from what the mirroring believes) and emits them as one "cookies"
// event.
class CookieDumper : public CefCookieVisitor {
 public:
  explicit CookieDumper(std::string jar) : jar_(std::move(jar)), list_(CefListValue::Create()) {}
  bool Visit(const CefCookie& c, int, int, bool&) override {
    auto d = CefDictionaryValue::Create();
    d->SetString("domain", CefString(&c.domain));
    d->SetString("path", CefString(&c.path));
    d->SetString("name", CefString(&c.name));
    d->SetString("value", CefString(&c.value));
    d->SetString("sameSite", SameSiteName(c.same_site));
    d->SetBool("httpOnly", c.httponly);
    list_->SetDictionary(list_->GetSize(), d);
    return true;
  }
  ~CookieDumper() override {
    // Released when the visit completes, including when there were none.
    auto d = CefDictionaryValue::Create();
    d->SetString("event", "cookies");
    d->SetString("jar", jar_);
    d->SetList("cookies", list_);
    Emit(d);
  }
 private:
  std::string jar_;
  CefRefPtr<CefListValue> list_;
  IMPLEMENT_REFCOUNTING(CookieDumper);
};

struct Jar {
  std::string name;
  CefRefPtr<CefRequestContext> context;  // null: the global (on-disk) profile
  std::map<std::string, CookieRec> snapshot;    // what both sides agree on
  std::map<std::string, double> recent_firefox; // key -> when Firefox set it
  bool sync_on = false, poll_running = false, poll_again = false;
  bool retired = false;  // an ended private session: never touch any store
  int ops_inflight = 0;
  std::vector<int> flush_reqs;

  CefRefPtr<CefCookieManager> Manager() {
    return context ? context->GetCookieManager(nullptr)
                   : CefCookieManager::GetGlobalManager(nullptr);
  }

  CefRefPtr<CefDictionaryValue> CookieEvent(const char* ev, const CookieRec& r) {
    auto d = Event(ev);
    d->SetString("jar", name);
    d->SetString("domain", r.domain);
    d->SetString("path", r.path);
    d->SetString("name", r.name);
    d->SetString("value", r.value);
    d->SetBool("secure", r.secure);
    d->SetBool("httpOnly", r.httponly);
    d->SetBool("session", r.session);
    d->SetDouble("expires", r.expires);
    d->SetString("sameSite", SameSiteName(r.same_site));
    return d;
  }

  void Apply(const CookieRec& r, std::function<void()> done) {
    if (retired) {
      if (done) done();
      return;
    }
    const std::string key = CookieKey(r);
    recent_firefox[key] = NowSecs();
    auto it = snapshot.find(key);
    if (it != snapshot.end() && SameCookie(it->second, r)) {
      if (done) done();
      return;
    }
    snapshot[key] = r;
    CefCookie c;
    CefString(&c.name) = r.name;
    CefString(&c.value) = r.value;
    // A leading dot means a domain cookie; host-only cookies leave it empty.
    if (!r.domain.empty() && r.domain[0] == '.') CefString(&c.domain) = r.domain;
    CefString(&c.path) = r.path.empty() ? "/" : r.path;
    c.secure = r.secure;
    c.httponly = r.httponly;
    c.has_expires = !r.session;
    if (!r.session) c.expires = FromEpoch(r.expires);
    c.same_site = (cef_cookie_same_site_t)r.same_site;
    ops_inflight++;
    auto finished = [this, done] {
      ops_inflight--;
      if (done) done();
    };
    if (!Manager()->SetCookie(CookieUrl(r), c, new SetDone(finished))) finished();
  }

  void Delete(const CookieRec& r) {
    if (retired) return;
    const std::string key = CookieKey(r);
    recent_firefox[key] = NowSecs();
    if (!snapshot.erase(key)) return;
    ops_inflight++;
    auto finished = [this] { ops_inflight--; };
    if (!Manager()->DeleteCookies(CookieUrl(r), r.name, new DeleteDone(finished)))
      finished();
  }

  // Replace this profile's cookies with Firefox's, then start mirroring.
  void Replace(std::vector<CookieRec> cookies) {
    if (retired) return;
    sync_on = false;
    snapshot.clear();
    ops_inflight++;
    auto list = std::make_shared<std::vector<CookieRec>>(std::move(cookies));
    auto after_delete = [this, list] {
      ops_inflight--;
      auto remaining = std::make_shared<size_t>(list->size());
      auto finish = [this] {
        sync_on = true;
        auto d = Event("cookies-ready");
        d->SetString("jar", name);
        d->SetInt("count", (int)snapshot.size());
        Emit(d);
      };
      if (list->empty()) {
        finish();
        return;
      }
      for (auto& r : *list) {
        Apply(r, [remaining, finish] {
          if (--*remaining == 0) finish();
        });
      }
    };
    if (!Manager()->DeleteCookies("", "", new DeleteDone(after_delete)))
      after_delete();
  }

  void StartPoll() {
    if (retired) return;
    if (poll_running) {
      poll_again = true;
      return;
    }
    poll_running = true;
    if (!Manager()->VisitAllCookies(new CookieCollector(this))) {
      poll_running = false;
      FinishPoll({});
    }
  }

  void FinishPoll(std::map<std::string, CookieRec> current) {
    poll_running = false;
    const double now = NowSecs();
    auto recently_from_firefox = [&](const std::string& key) {
      auto it = recent_firefox.find(key);
      return it != recent_firefox.end() && now - it->second < 3;
    };
    if (sync_on) {
      for (auto& [key, rec] : current) {
        auto it = snapshot.find(key);
        if (it != snapshot.end() && SameCookie(it->second, rec)) continue;
        if (recently_from_firefox(key)) continue;  // our own write landing
        snapshot[key] = rec;
        Emit(CookieEvent("cookie-set", rec));
      }
      for (auto it = snapshot.begin(); it != snapshot.end();) {
        if (current.count(it->first) || recently_from_firefox(it->first)) {
          ++it;
          continue;
        }
        Emit(CookieEvent("cookie-del", it->second));
        it = snapshot.erase(it);
      }
    }
    for (auto it = recent_firefox.begin(); it != recent_firefox.end();)
      it = (now - it->second > 10) ? recent_firefox.erase(it) : std::next(it);
    for (int req : flush_reqs) {
      auto d = Event("cookies-flushed");
      d->SetString("jar", name);
      d->SetInt("req", req);
      Emit(d);
    }
    flush_reqs.clear();
    if (poll_again) {
      poll_again = false;
      StartPoll();
    }
  }
};

std::map<std::string, std::unique_ptr<Jar>> g_jars;
// Ended private profiles: kept allocated (but inert) because cookie
// callbacks may still refer to them. One small object per private session.
std::vector<std::unique_ptr<Jar>> g_retired_jars;

Jar* GetJar(const std::string& name) {
  auto it = g_jars.find(name);
  if (it != g_jars.end()) return it->second.get();
  auto jar = std::make_unique<Jar>();
  jar->name = name;
  if (name == "private") {
    // Empty cache path: everything (cookies, storage, cache) stays in memory.
    CefRequestContextSettings s;
    jar->context = CefRequestContext::CreateContext(s, nullptr);
  }
  Jar* raw = jar.get();
  g_jars[name] = std::move(jar);
  return raw;
}

void FinishJarPoll(Jar* jar, std::map<std::string, CookieRec> current) {
  CEF_REQUIRE_UI_THREAD();
  jar->FinishPoll(std::move(current));
}

void CookiePollTick() {
  if (g_quitting) return;
  for (auto& [name, jar] : g_jars)
    if (jar->sync_on) jar->StartPoll();
  CefPostDelayedTask(TID_UI, base::BindOnce(&CookiePollTick), 1000);
}

int CookieOpsInflight() {
  int n = 0;
  for (auto& [name, jar] : g_jars) n += jar->ops_inflight;
  return n;
}

// ----------------------------------------------------------------- tabs --

struct Tab {
  CefRefPtr<CefBrowser> browser;
  // Firefox's toplevel uses a non-default visual and colormap; Chromium
  // creates its window with the default visual and the parent's colormap,
  // which X rejects (BadMatch). Each tab therefore lives in a container
  // window of ours that has the default visual and its own colormap.
  Window container = 0;
  int win = -1;
  std::string jar = "normal";
  int x = 0, y = 0, w = 100, h = 100;
  bool visible = false;
};

std::map<int, Tab> g_tabs;            // tab id -> tab
std::map<int, int> g_browser_to_tab;  // CEF browser id -> tab id
int g_focused_tab = -1;
// Popups get ids from a range Firefox never uses for its own tabs.
int g_next_popup_id = 1000000;
std::deque<int> g_pending_popups;  // made in OnBeforePopup, adopted in OnAfterCreated

int TabIdFor(CefRefPtr<CefBrowser> b) {
  auto it = g_browser_to_tab.find(b->GetIdentifier());
  return it == g_browser_to_tab.end() ? -1 : it->second;
}

Window XWindowFor(const Tab& t) {
  if (!t.browser) return 0;
  return (Window)t.browser->GetHost()->GetWindowHandle();
}

Win* WinFor(const Tab& t) {
  auto it = g_wins.find(t.win);
  return it == g_wins.end() ? nullptr : &it->second;
}

Window CreateContainer(const Tab& t) {
  Win* win = WinFor(t);
  if (!win) return 0;
  int scr = DefaultScreen(g_dpy);
  Visual* vis = DefaultVisual(g_dpy, scr);
  XSetWindowAttributes a = {};
  a.colormap = XCreateColormap(g_dpy, win->parent, vis, AllocNone);
  a.background_pixel = WhitePixel(g_dpy, scr);
  a.border_pixel = 0;
  Window c = XCreateWindow(g_dpy, win->parent, t.x + win->off_x,
                           t.y + win->off_y, std::max(1, t.w), std::max(1, t.h),
                           0, DefaultDepth(g_dpy, scr), InputOutput, vis,
                           CWColormap | CWBackPixel | CWBorderPixel, &a);
  if (t.visible) XMapRaised(g_dpy, c);
  XFlush(g_dpy);
  return c;
}

void ApplyGeometry(Tab& t) {
  Win* win = WinFor(t);
  if (!g_dpy || !t.container || !win) return;
  int w = std::max(1, t.w), h = std::max(1, t.h);
  XMoveResizeWindow(g_dpy, t.container, t.x + win->off_x, t.y + win->off_y, w, h);
  if (Window xw = XWindowFor(t)) {
    XMoveResizeWindow(g_dpy, xw, 0, 0, w, h);
    XMapWindow(g_dpy, xw);
  }
  if (t.visible) {
    XMapRaised(g_dpy, t.container);
  } else {
    XUnmapWindow(g_dpy, t.container);
  }
  XFlush(g_dpy);
  if (!t.browser) return;
  t.browser->GetHost()->NotifyMoveOrResizeStarted();
  t.browser->GetHost()->WasResized();
  t.browser->GetHost()->WasHidden(!t.visible);
}

void FocusFirefox(int win_id) {
  if (g_focused_tab >= 0) {
    auto it = g_tabs.find(g_focused_tab);
    if (it != g_tabs.end() && it->second.browser)
      it->second.browser->GetHost()->SetFocus(false);
  }
  g_focused_tab = -1;
  auto w = g_wins.find(win_id);
  if (w == g_wins.end() || !g_dpy) return;
  XSetInputFocus(g_dpy, w->second.focus_target ? w->second.focus_target
                                               : w->second.parent,
                 RevertToParent, CurrentTime);
  XFlush(g_dpy);
}

void FocusTab(int id) {
  auto it = g_tabs.find(id);
  if (it == g_tabs.end() || !it->second.browser || !it->second.visible) return;
  g_focused_tab = id;
  it->second.browser->GetHost()->SetFocus(true);
  if (Window xw = XWindowFor(it->second)) {
    XSetInputFocus(g_dpy, xw, RevertToParent, CurrentTime);
    XFlush(g_dpy);
  }
}

// Clicks don't reliably move X keyboard focus between an embedded child and
// its parent (the window manager only sees the toplevel). So watch the
// pointer: a press inside a Chromium view focuses it; a press elsewhere in a
// Firefox window hands the keyboard back to that window.
unsigned int g_last_buttons = 0;

void PollPointer() {
  if (g_quitting) return;
  if (g_dpy && !g_wins.empty()) {
    Window root = DefaultRootWindow(g_dpy), root_ret, top_child;
    int rx, ry, wx, wy;
    unsigned int mask = 0;
    if (XQueryPointer(g_dpy, root, &root_ret, &top_child, &rx, &ry, &wx, &wy,
                      &mask)) {
      const unsigned int buttons =
          mask & (Button1Mask | Button2Mask | Button3Mask);
      if (buttons && !g_last_buttons && top_child) {
        for (auto& [win_id, win] : g_wins) {
          if (TopAncestor(win.parent) != top_child) continue;
          int px = 0, py = 0;
          Window dummy;
          XTranslateCoordinates(g_dpy, root, win.parent, rx, ry, &px, &py, &dummy);
          int lx = px - win.off_x, ly = py - win.off_y;
          int hit = -1;
          for (auto& [id, t] : g_tabs) {
            if (t.win != win_id || !t.visible) continue;
            if (lx >= t.x && ly >= t.y && lx < t.x + t.w && ly < t.y + t.h) {
              hit = id;
              break;
            }
          }
          if (hit >= 0 && hit != g_focused_tab) {
            FocusTab(hit);
            Emit(Event("focused", hit));
          } else if (hit < 0 && g_focused_tab >= 0) {
            FocusFirefox(win_id);
            Emit(Event("unfocused"));
          }
          break;
        }
      }
      g_last_buttons = buttons;
    }
  }
  CefPostDelayedTask(TID_UI, base::BindOnce(&PollPointer), 30);
}

// ---------------------------------------------------------- JS dialogs --

std::map<int, std::pair<int, CefRefPtr<CefJSDialogCallback>>> g_dialogs;
int g_next_dialog = 1;

// -------------------------------------------------------- privacy checks --
// Every request a Chromium page makes is checked by Firefox: page loads
// against Safe Browsing (phishing, malware), third-party subresources against
// Enhanced Tracking Protection. Firefox's own lists and settings decide;
// answers are cached per (site, request host) for the session.

struct PendingCheck {
  std::string key;        // in-flight de-duplication (exact request)
  std::string cache_key;  // verdict cache (sites involved)
  std::vector<CefRefPtr<CefCallback>> callbacks;
  std::vector<CefRefPtr<CefRequest>> requests;
  int tab = -1;
  std::string url, kind;
};
std::map<int, PendingCheck> g_checks;          // req -> waiting requests
std::map<std::string, int> g_check_by_key;     // key -> req in flight
std::map<std::string, std::string> g_verdicts; // cache key -> "" (allow) or reason
int g_next_check = 1;
// While request-filtering Firefox extensions are installed, every request
// (including same-site ones) is checked, and verdicts are per request.
std::atomic<bool> g_check_all{false};

// The page each browser is loading, recorded when its main-frame request
// starts (and on redirects). A page's first subresource requests can arrive
// before the frame reports its new address, and without this they would be
// classified against no page at all. IO thread writes, IO thread reads.
std::mutex g_nav_mutex;
std::map<int, std::string> g_nav_url;  // CEF browser id -> main-frame URL

void SetNavUrl(int browser_id, const std::string& url) {
  std::lock_guard<std::mutex> lock(g_nav_mutex);
  g_nav_url[browser_id] = url;
}

std::string NavUrl(int browser_id) {
  std::lock_guard<std::mutex> lock(g_nav_mutex);
  auto it = g_nav_url.find(browser_id);
  return it == g_nav_url.end() ? "" : it->second;
}

// Responses an extension substituted (e.g. uBlock Origin's harmless stand-in
// scripts), served for that one request. Filled on the UI thread, consumed on
// the IO thread.
std::mutex g_subst_mutex;
std::map<uint64_t, std::pair<std::string, std::string>> g_substitutes;  // request id -> (mime, body)

std::string HostOf(const std::string& url) {
  size_t s = url.find("://");
  if (s == std::string::npos) return "";
  s += 3;
  size_t e = url.find_first_of("/?#", s);
  std::string host = url.substr(s, e == std::string::npos ? std::string::npos : e - s);
  size_t at = host.rfind('@');
  if (at != std::string::npos) host = host.substr(at + 1);
  size_t colon = host.rfind(':');
  if (colon != std::string::npos && host.find(']') == std::string::npos)
    host = host.substr(0, colon);
  return host;
}

bool IsWeb(const std::string& url) {
  return url.rfind("http://", 0) == 0 || url.rfind("https://", 0) == 0;
}

struct Verdict {
  std::string reason;          // non-empty: block
  bool cache = true;           // may be reused for the same sites
  std::string redirect_url;    // follow this instead
  bool has_body = false;       // serve this body instead
  std::string body, mime;
};
void ResolveCheckWith(int req, const Verdict& v, bool from_timeout);

// Firefox didn't answer in time (busy, hung, or the bridge broke). A page
// load is allowed, so a stall can never make browsing impossible; a
// subresource is blocked ("fail closed"), because allowing it would let
// trackers and ads through whenever Firefox is slow. HYBRID_FAIL_OPEN=1
// allows both, as earlier versions did.
void CheckTimedOut(int req) {
  auto it = g_checks.find(req);
  if (it == g_checks.end()) return;
  const bool allowed =
      it->second.kind == "navigation" || std::getenv("HYBRID_FAIL_OPEN") != nullptr;
  auto d = Event("check-timeout", it->second.tab);
  d->SetString("url", it->second.url);
  d->SetString("kind", it->second.kind);
  d->SetBool("allowed", allowed);
  Emit(d);
  Log(std::string("privacy check timed out, ") + (allowed ? "allowed: " : "blocked: ") +
      it->second.url);
  Verdict v;
  if (!allowed) v.reason = "no verdict";
  ResolveCheckWith(req, v, true);
}

void StartCheck(int browser_id, std::string url, std::string top,
                std::string kind, int type, CefRefPtr<CefRequest> request,
                CefRefPtr<CefCallback> callback) {
  CEF_REQUIRE_UI_THREAD();
  auto bt = g_browser_to_tab.find(browser_id);
  int tab = bt == g_browser_to_tab.end() ? -1 : bt->second;
  std::string jar = "normal";
  if (auto t = g_tabs.find(tab); t != g_tabs.end()) jar = t->second.jar;
  const std::string cache_key = jar + "|" + kind + "|" + HostOf(url) + "|" +
                                (kind == "navigation" ? "" : HostOf(top));
  const std::string key = jar + "|" + kind + "|" + url + "|" + HostOf(top);
  if (auto v = g_verdicts.find(cache_key); v != g_verdicts.end()) {
    if (v->second.empty()) {
      callback->Continue();
    } else {
      callback->Cancel();
      auto d = Event("blocked", tab);
      d->SetString("url", url);
      d->SetString("kind", kind);
      d->SetString("reason", v->second);
      Emit(d);
    }
    return;
  }
  if (auto f = g_check_by_key.find(key); f != g_check_by_key.end()) {
    g_checks[f->second].callbacks.push_back(callback);  // same question in flight
    g_checks[f->second].requests.push_back(request);
    return;
  }
  const int req = g_next_check++;
  PendingCheck& pc = g_checks[req];
  pc.key = key;
  pc.cache_key = cache_key;
  pc.callbacks.push_back(callback);
  pc.requests.push_back(request);
  pc.tab = tab;
  pc.url = url;
  pc.kind = kind;
  g_check_by_key[key] = req;
  auto d = Event("classify", tab);
  d->SetInt("req", req);
  d->SetString("kind", kind);
  d->SetString("url", url);
  d->SetString("top", top);
  d->SetString("jar", jar);
  d->SetInt("type", type);
  Emit(d);
  // Never hang a page on a missing answer (see CheckTimedOut).
  int timeout_ms = 1500;
  if (const char* e = std::getenv("HYBRID_CHECK_TIMEOUT_MS")) timeout_ms = std::max(50, std::atoi(e));
  CefPostDelayedTask(TID_UI, base::BindOnce(&CheckTimedOut, req), timeout_ms);
}

void ResolveCheckWith(int req, const Verdict& v, bool from_timeout) {
  auto it = g_checks.find(req);
  if (it == g_checks.end()) return;
  PendingCheck pc = std::move(it->second);
  g_checks.erase(it);
  g_check_by_key.erase(pc.key);
  // Timeouts and "no verdict" answers are never cached: the next request
  // asks again.
  if (!from_timeout && v.cache && v.reason != "no verdict" && v.redirect_url.empty() &&
      !v.has_body) {
    g_verdicts[pc.cache_key] = v.reason;
  }
  for (size_t i = 0; i < pc.callbacks.size(); i++) {
    if (!v.reason.empty()) {
      pc.callbacks[i]->Cancel();
      continue;
    }
    if (!v.redirect_url.empty()) {
      pc.requests[i]->SetURL(v.redirect_url);
    } else if (v.has_body) {
      std::lock_guard<std::mutex> lock(g_subst_mutex);
      g_substitutes[pc.requests[i]->GetIdentifier()] = {v.mime, v.body};
    }
    pc.callbacks[i]->Continue();
  }
  if (!v.reason.empty() || v.has_body || !v.redirect_url.empty()) {
    auto d = Event(pc.kind == "navigation" && !v.reason.empty() ? "unsafe" : "blocked", pc.tab);
    d->SetString("url", pc.url);
    d->SetString("kind", pc.kind);
    d->SetString("reason", !v.reason.empty() ? v.reason : "replaced by an extension");
    Emit(d);
  }
}

// --------------------------------------------------------------- client --

void MaybeFinishQuit();

class HostClient : public CefClient,
                   public CefDisplayHandler,
                   public CefLifeSpanHandler,
                   public CefLoadHandler,
                   public CefKeyboardHandler,
                   public CefJSDialogHandler,
                   public CefRequestHandler,
                   public CefResourceRequestHandler {
 public:
  CefRefPtr<CefRequestHandler> GetRequestHandler() override { return this; }
  CefRefPtr<CefResourceRequestHandler> GetResourceRequestHandler(
      CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>, CefRefPtr<CefRequest>, bool,
      bool, const CefString&, bool&) override {
    return this;
  }

  // IO thread. Same-host requests pass straight through; everything else
  // waits for Firefox's verdict (see "privacy checks").
  ReturnValue OnBeforeResourceLoad(CefRefPtr<CefBrowser> browser,
                                   CefRefPtr<CefFrame> frame,
                                   CefRefPtr<CefRequest> request,
                                   CefRefPtr<CefCallback> callback) override {
    if (std::getenv("HYBRID_NO_PROTECTION") || !browser) return RV_CONTINUE;
    const std::string url = request->GetURL().ToString();
    const bool nav = request->GetResourceType() == RT_MAIN_FRAME;
    if (nav) SetNavUrl(browser->GetIdentifier(), url);  // even for data: pages
    if (!IsWeb(url)) return RV_CONTINUE;
    std::string top;
    if (!nav) {
      CefRefPtr<CefFrame> main = browser->GetMainFrame();
      top = main ? main->GetURL().ToString() : "";
      // Not committed yet: use the page being loaded. If even that isn't a
      // web page (data:, about:blank), the request is still checked, with
      // no top page; it is never let through unchecked.
      if (!IsWeb(top)) top = NavUrl(browser->GetIdentifier());
      if (!IsWeb(top)) top.clear();
      if (!g_check_all && !top.empty() && HostOf(top) == HostOf(url)) return RV_CONTINUE;
    }
    CefPostTask(TID_UI, base::BindOnce(&StartCheck, browser->GetIdentifier(),
                                       url, top,
                                       std::string(nav ? "navigation" : "resource"),
                                       (int)request->GetResourceType(), request,
                                       callback));
    return RV_CONTINUE_ASYNC;
  }

  // IO thread: a redirected page load changes the page being loaded.
  void OnResourceRedirect(CefRefPtr<CefBrowser> browser, CefRefPtr<CefFrame>,
                          CefRefPtr<CefRequest> request, CefRefPtr<CefResponse>,
                          CefString& new_url) override {
    if (browser && request->GetResourceType() == RT_MAIN_FRAME)
      SetNavUrl(browser->GetIdentifier(), new_url.ToString());
  }

  // IO thread: serve a response an extension substituted for this request.
  CefRefPtr<CefResourceHandler> GetResourceHandler(
      CefRefPtr<CefBrowser>, CefRefPtr<CefFrame>,
      CefRefPtr<CefRequest> request) override {
    std::pair<std::string, std::string> sub;
    {
      std::lock_guard<std::mutex> lock(g_subst_mutex);
      auto it = g_substitutes.find(request->GetIdentifier());
      if (it == g_substitutes.end()) return nullptr;
      sub = std::move(it->second);
      g_substitutes.erase(it);
    }
    auto data = std::make_shared<std::string>(std::move(sub.second));
    CefRefPtr<CefStreamReader> reader =
        CefStreamReader::CreateForData((void*)data->data(), data->size());
    // The reader doesn't own the bytes; keep them alive with the handler.
    struct Holder : public CefStreamResourceHandler {
      Holder(const std::string& mime, CefRefPtr<CefStreamReader> r,
             std::shared_ptr<std::string> d)
          : CefStreamResourceHandler(mime, r), data_(std::move(d)) {}
      std::shared_ptr<std::string> data_;
    };
    std::string mime = sub.first.empty() ? "application/octet-stream" : sub.first;
    return new Holder(mime, reader, data);
  }
  CefRefPtr<CefJSDialogHandler> GetJSDialogHandler() override { return this; }
  CefRefPtr<CefDisplayHandler> GetDisplayHandler() override { return this; }
  CefRefPtr<CefLifeSpanHandler> GetLifeSpanHandler() override { return this; }
  CefRefPtr<CefLoadHandler> GetLoadHandler() override { return this; }
  CefRefPtr<CefKeyboardHandler> GetKeyboardHandler() override { return this; }

  // Display
  void OnTitleChange(CefRefPtr<CefBrowser> b, const CefString& title) override {
    auto d = Event("title", TabIdFor(b));
    d->SetString("title", title);
    Emit(d);
  }
  void OnAddressChange(CefRefPtr<CefBrowser> b, CefRefPtr<CefFrame> f,
                       const CefString& url) override {
    if (!f->IsMain()) return;
    auto d = Event("url", TabIdFor(b));
    d->SetString("url", url);
    Emit(d);
  }
  void OnFaviconURLChange(CefRefPtr<CefBrowser> b,
                          const std::vector<CefString>& urls) override {
    if (urls.empty()) return;
    auto d = Event("favicon", TabIdFor(b));
    d->SetString("url", urls[0]);
    Emit(d);
  }

  // Load
  void OnLoadingStateChange(CefRefPtr<CefBrowser> b, bool loading, bool back,
                            bool fwd) override {
    auto d = Event("loading", TabIdFor(b));
    d->SetBool("loading", loading);
    d->SetBool("canGoBack", back);
    d->SetBool("canGoForward", fwd);
    Emit(d);
  }

  // Life span
  void OnAfterCreated(CefRefPtr<CefBrowser> b) override {
    int id = pending_id_;
    if (b->IsPopup() && !g_pending_popups.empty()) {
      id = g_pending_popups.front();
      g_pending_popups.pop_front();
    }
    g_browser_to_tab[b->GetIdentifier()] = id;
    auto& t = g_tabs[id];
    t.browser = b;
    ApplyGeometry(t);
    if (t.visible) FocusTab(id);
    Emit(Event("created", id));
  }
  bool OnBeforePopup(CefRefPtr<CefBrowser> b, CefRefPtr<CefFrame>, int,
                     const CefString& url, const CefString&,
                     CefLifeSpanHandler::WindowOpenDisposition disposition, bool,
                     const CefPopupFeatures&, CefWindowInfo& window_info,
                     CefRefPtr<CefClient>&, CefBrowserSettings&,
                     CefRefPtr<CefDictionaryValue>&, bool*) override {
    // Keep the popup alive so window.opener and postMessage keep working
    // (sign-in flows depend on this), but host it as a new tab in the
    // opener's window. Popups share the opener's profile, so a private
    // tab's popup stays private.
    int opener = TabIdFor(b);
    auto op = g_tabs.find(opener);
    if (op == g_tabs.end()) return true;
    int id = g_next_popup_id++;
    Tab& t = g_tabs[id];
    t.win = op->second.win;
    t.jar = op->second.jar;
    t.x = op->second.x;
    t.y = op->second.y;
    t.w = op->second.w;
    t.h = op->second.h;
    t.visible = false;
    t.container = CreateContainer(t);
    window_info.SetAsChild((CefWindowHandle)t.container,
                           CefRect(0, 0, std::max(1, t.w), std::max(1, t.h)));
    window_info.runtime_style = CEF_RUNTIME_STYLE_ALLOY;
    g_pending_popups.push_back(id);
    auto d = Event("popup", id);
    d->SetInt("opener", opener);
    d->SetString("url", url);
    d->SetBool("background", disposition == CEF_WOD_NEW_BACKGROUND_TAB);
    Emit(d);
    return false;
  }
  void OnBeforeClose(CefRefPtr<CefBrowser> b) override {
    int id = TabIdFor(b);
    g_browser_to_tab.erase(b->GetIdentifier());
    {
      std::lock_guard<std::mutex> lock(g_nav_mutex);
      g_nav_url.erase(b->GetIdentifier());
    }
    auto it = g_tabs.find(id);
    if (it != g_tabs.end()) {
      if (it->second.container) {
        XDestroyWindow(g_dpy, it->second.container);
        XFlush(g_dpy);
      }
      g_tabs.erase(it);
    }
    if (g_focused_tab == id) g_focused_tab = -1;
    Emit(Event("closed", id));
    MaybeFinishQuit();
  }

  // Keyboard: browser-level shortcuts belong to the shell.
  bool OnPreKeyEvent(CefRefPtr<CefBrowser> b, const CefKeyEvent& e,
                     CefEventHandle, bool*) override {
    if (e.type != KEYEVENT_RAWKEYDOWN) return false;
    const bool ctrl = e.modifiers & EVENTFLAG_CONTROL_DOWN;
    const bool shift = e.modifiers & EVENTFLAG_SHIFT_DOWN;
    const bool alt = e.modifiers & EVENTFLAG_ALT_DOWN;
    const char* key = nullptr;
    switch (e.windows_key_code) {
      case 0x54: if (ctrl) key = shift ? "ctrl+shift+t" : "ctrl+t"; break;
      case 0x57: if (ctrl) key = "ctrl+w"; break;
      case 0x4C: if (ctrl) key = "ctrl+l"; break;
      case 0x4E: if (ctrl) key = shift ? "ctrl+shift+n" : "ctrl+n"; break;
      case 0x50: if (ctrl && shift) key = "ctrl+shift+p"; break;
      case 0x09: if (ctrl) key = shift ? "ctrl+shift+tab" : "ctrl+tab"; break;
      case 0x21: if (ctrl) key = "ctrl+pageup"; break;
      case 0x22: if (ctrl) key = "ctrl+pagedown"; break;
      case 0x45: if (ctrl && alt) key = "ctrl+alt+e"; break;
      case 0x75: key = "f6"; break;
      case 0x74: key = shift ? "shift+f5" : "f5"; break;
      case 0x52: if (ctrl) key = shift ? "ctrl+shift+r" : "ctrl+r"; break;
      case 0xBB: case 0x6B: if (ctrl) key = "zoom-in"; break;    // = / numpad +
      case 0xBD: case 0x6D: if (ctrl) key = "zoom-out"; break;   // - / numpad -
      case 0x30: case 0x60: if (ctrl) key = "zoom-reset"; break; // 0 / numpad 0
    }
    if (!key) return false;
    auto d = Event("shortcut", TabIdFor(b));
    d->SetString("key", key);
    Emit(d);
    return true;
  }

  // JavaScript alert/confirm/prompt and "leave this page?" dialogs: Firefox
  // shows them and answers with a dialog-reply command.
  bool OnJSDialog(CefRefPtr<CefBrowser> b, const CefString& origin,
                  JSDialogType type, const CefString& message,
                  const CefString& default_text,
                  CefRefPtr<CefJSDialogCallback> callback, bool&) override {
    int req = g_next_dialog++;
    g_dialogs[req] = {TabIdFor(b), callback};
    auto d = Event("dialog", TabIdFor(b));
    d->SetInt("req", req);
    d->SetString("type", type == JSDIALOGTYPE_ALERT     ? "alert"
                         : type == JSDIALOGTYPE_CONFIRM ? "confirm"
                                                        : "prompt");
    d->SetString("message", message);
    d->SetString("defaultText", default_text);
    d->SetString("origin", origin);
    Emit(d);
    return true;
  }
  bool OnBeforeUnloadDialog(CefRefPtr<CefBrowser> b, const CefString&,
                            bool is_reload,
                            CefRefPtr<CefJSDialogCallback> callback) override {
    int req = g_next_dialog++;
    g_dialogs[req] = {TabIdFor(b), callback};
    auto d = Event("dialog", TabIdFor(b));
    d->SetInt("req", req);
    d->SetString("type", "beforeunload");
    d->SetBool("isReload", is_reload);
    Emit(d);
    return true;
  }
  void OnResetDialogState(CefRefPtr<CefBrowser> b) override {
    int id = TabIdFor(b);
    for (auto it = g_dialogs.begin(); it != g_dialogs.end();) {
      if (it->second.first == id) {
        auto d = Event("dialog-cancelled", id);
        d->SetInt("req", it->first);
        Emit(d);
        it = g_dialogs.erase(it);
      } else {
        ++it;
      }
    }
  }

  void set_pending_id(int id) { pending_id_ = id; }

 private:
  int pending_id_ = -1;
  IMPLEMENT_REFCOUNTING(HostClient);
};

CefRefPtr<HostClient> g_client;

void MaybeFinishQuit() {
  if (g_quitting && g_tabs.empty()) CefQuitMessageLoop();
}

void CloseTab(Tab& t) {
  if (t.browser) {
    t.browser->GetHost()->CloseBrowser(true);
  } else if (t.container) {
    XDestroyWindow(g_dpy, t.container);
    t.container = 0;
  }
}

// ------------------------------------------------------------- commands --

void ReadRect(CefRefPtr<CefDictionaryValue> c, Tab& t) {
  if (c->HasKey("x")) t.x = c->GetInt("x");
  if (c->HasKey("y")) t.y = c->GetInt("y");
  if (c->HasKey("w")) t.w = c->GetInt("w");
  if (c->HasKey("h")) t.h = c->GetInt("h");
}

std::string JarName(CefRefPtr<CefDictionaryValue> c) {
  return c->HasKey("jar") ? c->GetString("jar").ToString() : "normal";
}

void HandleLine(const std::string& line);

// Attaching looks for the window whose title Firefox has set to a marker.
// Title changes reach the X server asynchronously, so retry for a while.
void TryAttach(int win_id, std::string marker, int attempt) {
  Window w = FindByTitle(DefaultRootWindow(g_dpy), marker, 0);
  if (!w) {
    if (attempt < 60) {
      CefPostDelayedTask(TID_UI,
                         base::BindOnce(&TryAttach, win_id, marker, attempt + 1),
                         50);
    } else {
      auto d = Event("error");
      d->SetInt("win", win_id);
      d->SetString("msg", "Firefox window not found. Is Firefox running on "
                          "X11 (MOZ_ENABLE_WAYLAND=0)?");
      Emit(d);
    }
    return;
  }
  Win& win = g_wins[win_id];
  win.parent = w;
  ComputeWinLayout(win);
  auto d = Event("attached");
  d->SetInt("win", win_id);
  d->SetDouble("xid", (double)w);
  d->SetInt("offsetX", win.off_x);
  d->SetInt("offsetY", win.off_y);
  d->SetBool("focusProxy", win.focus_target != 0);
  Emit(d);
}

void HandleLine(const std::string& line) {
  CEF_REQUIRE_UI_THREAD();
  CefRefPtr<CefValue> v = CefParseJSON(line, JSON_PARSER_RFC);
  if (!v || v->GetType() != VTYPE_DICTIONARY) {
    Log("bad command: " + line);
    return;
  }
  auto c = v->GetDictionary();
  const std::string cmd = c->GetString("cmd");
  const int id = c->HasKey("id") ? c->GetInt("id") : -1;

  // ---- session-wide commands
  if (cmd == "attach") {
    TryAttach(c->GetInt("win"), c->GetString("marker"), 0);
    return;
  }
  if (cmd == "detach") {
    int win_id = c->GetInt("win");
    for (auto& [tid, t] : g_tabs)
      if (t.win == win_id) CloseTab(t);
    g_wins.erase(win_id);
    return;
  }
  if (cmd == "quit") {
    if (g_quitting) return;
    g_quitting = true;
    if (g_tabs.empty()) {
      CefQuitMessageLoop();
    } else {
      for (auto& [tid, t] : g_tabs) CloseTab(t);
    }
    return;
  }
  if (cmd == "unfocus") {
    FocusFirefox(c->HasKey("win") ? c->GetInt("win") : -1);
    return;
  }
  if (cmd == "cookies-replace") {
    std::vector<CookieRec> list;
    auto arr = c->GetList("cookies");
    for (size_t i = 0; arr && i < arr->GetSize(); i++)
      list.push_back(ReadCookieRec(arr->GetDictionary(i)));
    GetJar(JarName(c))->Replace(std::move(list));
    return;
  }
  if (cmd == "cookie-set") {
    GetJar(JarName(c))->Apply(ReadCookieRec(c), nullptr);
    return;
  }
  if (cmd == "cookie-del") {
    GetJar(JarName(c))->Delete(ReadCookieRec(c));
    return;
  }
  if (cmd == "cookies-flush") {
    Jar* jar = GetJar(JarName(c));
    jar->flush_reqs.push_back(c->GetInt("req"));
    jar->StartPoll();
    return;
  }
  if (cmd == "cookies-dump") {
    Jar* jar = GetJar(JarName(c));
    if (jar->retired || !jar->Manager()->VisitAllCookies(new CookieDumper(jar->name))) {
      // No store (or nothing to visit): report an empty jar.
      auto d = Event("cookies");
      d->SetString("jar", jar->name);
      d->SetList("cookies", CefListValue::Create());
      Emit(d);
    }
    return;
  }
  if (cmd == "private-end") {
    // The last private window closed: forget the in-memory profile entirely
    // (cookies, storage, cache). The next private window gets a fresh one.
    for (auto& [tid, t] : g_tabs)
      if (t.jar == "private") CloseTab(t);
    auto pj = g_jars.find("private");
    if (pj != g_jars.end()) {
      pj->second->retired = true;
      pj->second->sync_on = false;
      pj->second->poll_again = false;
      pj->second->snapshot.clear();
      pj->second->context = nullptr;  // drop our reference to the profile
      g_retired_jars.push_back(std::move(pj->second));
      g_jars.erase(pj);
    }
    Emit(Event("private-ended"));
    return;
  }
  if (cmd == "classified") {
    Verdict v;
    if (c->HasKey("reason")) v.reason = c->GetString("reason");
    if (c->HasKey("cache")) v.cache = c->GetBool("cache");
    if (c->HasKey("redirectURL")) v.redirect_url = c->GetString("redirectURL");
    if (c->HasKey("redirectBody")) {
      CefRefPtr<CefBinaryValue> bin = CefBase64Decode(c->GetString("redirectBody"));
      v.has_body = true;
      if (bin && bin->GetSize()) {
        v.body.resize(bin->GetSize());
        bin->GetData(&v.body[0], v.body.size(), 0);
      }
      if (c->HasKey("redirectMime")) v.mime = c->GetString("redirectMime");
    }
    ResolveCheckWith(c->GetInt("req"), v, false);
    return;
  }
  if (cmd == "bridge-mode") {
    g_check_all = c->GetBool("extensions");
    g_verdicts.clear();
    return;
  }
  if (cmd == "protection-reset") {
    // Firefox's protection settings or exceptions changed: forget verdicts.
    g_verdicts.clear();
    return;
  }
  if (cmd == "dialog-reply") {
    auto dit = g_dialogs.find(c->GetInt("req"));
    if (dit != g_dialogs.end()) {
      // Take the callback out first: Continue() synchronously triggers
      // OnResetDialogState, which also edits g_dialogs.
      CefRefPtr<CefJSDialogCallback> cb = dit->second.second;
      g_dialogs.erase(dit);
      cb->Continue(c->GetBool("ok"), c->GetString("text"));
    }
    return;
  }

  // ---- tab commands
  if (cmd == "create") {
    if (CookieOpsInflight() > 0) {
      // Let pending cookie writes land so the first request carries them.
      CefPostDelayedTask(TID_UI, base::BindOnce(&HandleLine, line), 20);
      return;
    }
    const int win_id = c->GetInt("win");
    if (!g_wins.count(win_id)) {
      Log("create for unattached window " + std::to_string(win_id));
      return;
    }
    Tab& t = g_tabs[id];
    t.win = win_id;
    t.jar = JarName(c);
    ReadRect(c, t);
    t.visible = c->HasKey("visible") ? c->GetBool("visible") : true;
    t.container = CreateContainer(t);
    CefWindowInfo info;
    info.SetAsChild((CefWindowHandle)t.container,
                    CefRect(0, 0, std::max(1, t.w), std::max(1, t.h)));
    info.runtime_style = CEF_RUNTIME_STYLE_ALLOY;
    CefBrowserSettings settings;
    g_client->set_pending_id(id);
    CefRefPtr<CefRequestContext> ctx = GetJar(t.jar)->context;
    CefRefPtr<CefBrowser> created = CefBrowserHost::CreateBrowserSync(
        info, g_client, c->GetString("url"), settings, nullptr, ctx);
    if (!created) {
      XDestroyWindow(g_dpy, t.container);
      g_tabs.erase(id);
      auto d = Event("error", id);
      d->SetString("msg", "CreateBrowserSync failed");
      Emit(d);
    }
    return;
  }

  auto it = g_tabs.find(id);
  if (it == g_tabs.end()) {
    if (id >= 0) Log("unknown tab " + std::to_string(id) + " for " + cmd);
    return;
  }
  Tab& t = it->second;
  CefRefPtr<CefBrowser> b = t.browser;

  if (cmd == "bounds") {
    ReadRect(c, t);
    ApplyGeometry(t);
  } else if (cmd == "show") {
    ReadRect(c, t);
    t.visible = true;
    ApplyGeometry(t);
  } else if (cmd == "hide") {
    t.visible = false;
    if (g_focused_tab == id) FocusFirefox(t.win);
    ApplyGeometry(t);
  } else if (cmd == "close") {
    if (g_focused_tab == id) FocusFirefox(t.win);
    CloseTab(t);
  } else if (!b) {
    // Not created yet; geometry is applied when it is.
  } else if (cmd == "navigate") {
    b->GetMainFrame()->LoadURL(c->GetString("url"));
  } else if (cmd == "back") {
    b->GoBack();
  } else if (cmd == "forward") {
    b->GoForward();
  } else if (cmd == "reload") {
    if (c->HasKey("skipCache") && c->GetBool("skipCache")) {
      b->ReloadIgnoreCache();
    } else {
      b->Reload();
    }
  } else if (cmd == "stop") {
    b->StopLoad();
  } else if (cmd == "zoom") {
    b->GetHost()->SetZoomLevel(c->GetDouble("level"));
    // Make the embedded view re-layout and repaint at the new zoom now.
    b->GetHost()->WasResized();
    b->GetHost()->Invalidate(PET_VIEW);
  } else if (cmd == "focus") {
    FocusTab(id);
  } else {
    Log("unknown command: " + cmd);
  }
}

void StdinThread() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    CefPostTask(TID_UI, base::BindOnce(&HandleLine, line));
  }
  // Firefox went away: shut down with it.
  CefPostTask(TID_UI,
              base::BindOnce(&HandleLine, std::string("{\"cmd\":\"quit\"}")));
}

// ------------------------------------------------------------------ app --

// Space-separated switches; double quotes keep a value with spaces together,
// e.g. --host-resolver-rules="MAP *.test 127.0.0.1".
void AppendFlags(CefRefPtr<CefCommandLine> command_line, const std::string& all) {
  std::string tok;
  bool quoted = false;
  for (size_t i = 0; i <= all.size(); i++) {
    if (i < all.size() && all[i] == '"') {
      quoted = !quoted;
      continue;
    }
    if (i == all.size() || (all[i] == ' ' && !quoted)) {
      if (tok.rfind("--", 0) == 0) {
        std::string sw = tok.substr(2);
        size_t eq = sw.find('=');
        if (eq == std::string::npos) {
          command_line->AppendSwitch(sw);
        } else {
          command_line->AppendSwitchWithValue(sw.substr(0, eq), sw.substr(eq + 1));
        }
      }
      tok.clear();
    } else {
      tok += all[i];
    }
  }
}

class HostApp : public CefApp, public CefBrowserProcessHandler {
 public:
  CefRefPtr<CefBrowserProcessHandler> GetBrowserProcessHandler() override {
    return this;
  }
  void OnBeforeCommandLineProcessing(
      const CefString&, CefRefPtr<CefCommandLine> command_line) override {
    // Windowed embedding needs X11; under Wayland this runs on XWayland.
    command_line->AppendSwitchWithValue("ozone-platform", "x11");
    // Keep Chromium's own keyring prompts out of the way in the prototype.
    command_line->AppendSwitchWithValue("password-store", "basic");
    // Animate wheel scrolling like Chrome on Linux does.
    command_line->AppendSwitch("enable-smooth-scrolling");
    // Render at exactly Firefox's scale, so Chromium tabs match Gecko tabs
    // instead of guessing the screen scale on their own.
    if (const char* scale = std::getenv("HYBRID_SCALE")) {
      double s = std::atof(scale);
      if (s >= 0.5 && s <= 8)
        command_line->AppendSwitchWithValue("force-device-scale-factor", scale);
    }
    // Extra Chromium switches for experiments, e.g.
    //   HYBRID_CHROMIUM_FLAGS="--disable-gpu"
    if (const char* extra = std::getenv("HYBRID_CHROMIUM_FLAGS"))
      AppendFlags(command_line, extra);
  }
  void OnContextInitialized() override {
    g_client = new HostClient();
    std::thread(StdinThread).detach();
    CookiePollTick();
    PollPointer();
    Emit(Event("ready"));
  }

 private:
  IMPLEMENT_REFCOUNTING(HostApp);
};

// ---------------------------------------------------- RAM-only profile --
// Unless HYBRID_CHROMIUM_PERSIST is set, Chromium's whole working folder
// lives in $XDG_RUNTIME_DIR (memory-backed, cleared at logout) and is deleted
// when the engine exits. Sign-ins come from Firefox's cookie jar and visits
// are recorded in Firefox's history, so nothing needs to persist here.

std::string g_ram_root;

int RemoveEntry(const char* path, const struct stat*, int, struct FTW*) {
  return remove(path);
}

void RemoveTree(const std::string& dir) {
  if (!dir.empty()) nftw(dir.c_str(), RemoveEntry, 16, FTW_DEPTH | FTW_PHYS);
}

// Folders named hybrid-chromium-<pid>-* whose process is gone are leftovers
// from a crash.
void RemoveStaleRamRoots(const std::string& base) {
  DIR* d = opendir(base.c_str());
  if (!d) return;
  while (struct dirent* e = readdir(d)) {
    std::string name = e->d_name;
    const std::string prefix = "hybrid-chromium-";
    if (name.rfind(prefix, 0) != 0) continue;
    long pid = std::atol(name.c_str() + prefix.size());
    if (pid > 0 && kill((pid_t)pid, 0) != 0) RemoveTree(base + "/" + name);
  }
  closedir(d);
}

std::string MakeRamRoot() {
  const char* runtime = std::getenv("XDG_RUNTIME_DIR");
  std::string base = (runtime && *runtime) ? runtime : "/tmp";
  RemoveStaleRamRoots(base);
  std::string tmpl = base + "/hybrid-chromium-" + std::to_string(getpid()) + "-XXXXXX";
  std::vector<char> buf(tmpl.begin(), tmpl.end());
  buf.push_back('\0');
  if (!mkdtemp(buf.data())) return "";
  if (!runtime || !*runtime) {
    std::fprintf(stderr, "chromium_host: XDG_RUNTIME_DIR not set; using %s "
                         "(may be on disk; still deleted on exit)\n", buf.data());
  }
  return std::string(buf.data());
}

int XErrorHandlerImpl(Display*, XErrorEvent*) {
  // A tab or window can vanish between our lookup and the X request.
  return 0;
}

}  // namespace

int main(int argc, char* argv[]) {
  CefMainArgs main_args(argc, argv);
  CefRefPtr<HostApp> app = new HostApp();

  // Renderer, GPU and utility subprocesses re-enter here and stop.
  int exit_code = CefExecuteProcess(main_args, app, nullptr);
  if (exit_code >= 0) return exit_code;

  XSetErrorHandler(XErrorHandlerImpl);
  g_dpy = XOpenDisplay(nullptr);
  if (!g_dpy) {
    std::fprintf(stderr, "chromium_host: cannot open X display\n");
    return 1;
  }

  CefSettings settings;
  // Resolve resources next to our own binary, wherever it was launched from.
  {
    char exe[4096] = {0};
    ssize_t n = readlink("/proc/self/exe", exe, sizeof(exe) - 1);
    std::string dir = n > 0 ? std::string(exe, n) : std::string(argv[0]);
    dir = dir.substr(0, dir.rfind('/'));
    CefString(&settings.resources_dir_path) = dir;
    CefString(&settings.locales_dir_path) = dir + "/locales";
  }
  settings.no_sandbox = std::getenv("HYBRID_NO_SANDBOX") != nullptr;
  settings.multi_threaded_message_loop = false;
  settings.log_severity = LOGSEVERITY_WARNING;
  std::string root;
  if (std::getenv("HYBRID_CHROMIUM_PERSIST")) {
    const char* profile = std::getenv("HYBRID_CHROMIUM_PROFILE");
    root = profile ? profile : "/tmp/hybrid-chromium-profile";
  } else {
    g_ram_root = MakeRamRoot();
    root = g_ram_root.empty() ? "/tmp/hybrid-chromium-fallback" : g_ram_root;
  }
  CefString(&settings.root_cache_path) = root;
  CefString(&settings.cache_path) = root + "/Default";
  if (const char* ua = std::getenv("HYBRID_USER_AGENT"))
    CefString(&settings.user_agent) = ua;

  if (!CefInitialize(main_args, settings, app, nullptr)) {
    std::fprintf(stderr, "chromium_host: CefInitialize failed\n");
    return 1;
  }
  CefRunMessageLoop();
  CefShutdown();
  XCloseDisplay(g_dpy);
  RemoveTree(g_ram_root);
  // The stdin reader thread is blocked in read(); skip static destructors.
  std::fflush(stdout);
  std::fflush(stderr);
  _exit(0);
}
