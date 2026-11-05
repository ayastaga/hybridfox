/* webkit_host: the WebKit engine of the hybrid browser (WebKitGTK).
 *
 * Speaks the same line protocol as chromium_host: JSON commands on stdin,
 * "@@"-prefixed JSON events on stdout. Each WebKit "tab" is a WebKitWebView
 * in a GtkPlug living in an X11 container window that sits over the content
 * area of a Firefox window.
 *
 * Profiles ("jars"): "normal" and "private" are both ephemeral WebKit
 * contexts (nothing is written to disk). Sign-ins come from Firefox's cookie
 * jar, mirrored both ways. The private context is replaced at every new
 * private session.
 */
#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <gdk/gdkx.h>
#include <gtk/gtk.h>
#include <gtk/gtkx.h>
#include <json-glib/json-glib.h>
#include <libsoup/soup.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <webkit2/webkit2.h>

/* ------------------------------------------------------------------ out -- */

static void emit(JsonBuilder *b) {
  json_builder_end_object(b);
  JsonGenerator *g = json_generator_new();
  JsonNode *root = json_builder_get_root(b);
  json_generator_set_root(g, root);
  gchar *s = json_generator_to_data(g, NULL);
  fprintf(stdout, "@@%s\n", s);
  fflush(stdout);
  g_free(s);
  json_node_unref(root);
  g_object_unref(g);
  g_object_unref(b);
}

static JsonBuilder *event(const char *name, int id) {
  JsonBuilder *b = json_builder_new();
  json_builder_begin_object(b);
  json_builder_set_member_name(b, "event");
  json_builder_add_string_value(b, name);
  if (id >= 0) {
    json_builder_set_member_name(b, "id");
    json_builder_add_int_value(b, id);
  }
  return b;
}

static void add_str(JsonBuilder *b, const char *k, const char *v) {
  json_builder_set_member_name(b, k);
  json_builder_add_string_value(b, v ? v : "");
}
static void add_int(JsonBuilder *b, const char *k, gint64 v) {
  json_builder_set_member_name(b, k);
  json_builder_add_int_value(b, v);
}
static void add_bool(JsonBuilder *b, const char *k, gboolean v) {
  json_builder_set_member_name(b, k);
  json_builder_add_boolean_value(b, v);
}
static void add_double(JsonBuilder *b, const char *k, double v) {
  json_builder_set_member_name(b, k);
  json_builder_add_double_value(b, v);
}

static void log_msg(const char *msg) {
  JsonBuilder *b = event("log", -1);
  add_str(b, "msg", msg);
  emit(b);
}

static gboolean g_quitting = FALSE;
static double g_scale = 1.0;  /* Firefox's device scale */

/* ------------------------------------------------------------------ X11 -- */

static Display *dpy;

typedef struct {
  int id;
  Window parent, focus_target;
  int off_x, off_y;
} Win;
static GHashTable *wins; /* id -> Win* */

static char *get_name(Window w) {
  Atom net = XInternAtom(dpy, "_NET_WM_NAME", True);
  Atom utf8 = XInternAtom(dpy, "UTF8_STRING", True);
  if (net != None && utf8 != None) {
    Atom type;
    int format;
    unsigned long n, after;
    unsigned char *data = NULL;
    if (XGetWindowProperty(dpy, w, net, 0, 1024, False, utf8, &type, &format, &n, &after, &data) == Success && data) {
      char *out = g_strndup((char *)data, n);
      XFree(data);
      return out;
    }
    if (data) XFree(data);
  }
  char *name = NULL;
  if (XFetchName(dpy, w, &name) && name) {
    char *out = g_strdup(name);
    XFree(name);
    return out;
  }
  return NULL;
}

static Window find_by_title(Window w, const char *marker, int depth) {
  if (depth > 5) return 0;
  if (depth > 0) {
    char *name = get_name(w);
    gboolean hit = name && strstr(name, marker);
    g_free(name);
    if (hit) return w;
  }
  Window root, parent, *children = NULL;
  unsigned int n = 0;
  Window found = 0;
  if (XQueryTree(dpy, w, &root, &parent, &children, &n)) {
    for (unsigned int i = 0; i < n && !found; i++) found = find_by_title(children[i], marker, depth + 1);
    if (children) XFree(children);
  }
  return found;
}

static void compute_layout(Win *win) {
  win->off_x = win->off_y = 0;
  win->focus_target = 0;
  Window root, parent, *children = NULL;
  unsigned int n = 0;
  long best = 0;
  if (XQueryTree(dpy, win->parent, &root, &parent, &children, &n)) {
    for (unsigned int i = 0; i < n; i++) {
      XWindowAttributes a;
      if (!XGetWindowAttributes(dpy, children[i], &a)) continue;
      if (a.class == InputOnly && a.width == 1 && a.height == 1) {
        if (!win->focus_target) win->focus_target = children[i];
        continue;
      }
      if (a.class == InputOutput && a.map_state == IsViewable && !a.override_redirect &&
          (long)a.width * a.height > best) {
        best = (long)a.width * a.height;
        win->off_x = a.x;
        win->off_y = a.y;
      }
    }
    if (children) XFree(children);
  }
  const char *e;
  if ((e = getenv("HYBRID_OFFSET_X"))) win->off_x += atoi(e);
  if ((e = getenv("HYBRID_OFFSET_Y"))) win->off_y += atoi(e);
}

static Window top_ancestor(Window w) {
  Window cur = w;
  for (int i = 0; i < 8; i++) {
    Window root, parent, *children = NULL;
    unsigned int n = 0;
    if (!XQueryTree(dpy, cur, &root, &parent, &children, &n)) return 0;
    if (children) XFree(children);
    if (parent == root || parent == 0) return cur;
    cur = parent;
  }
  return cur;
}

/* ----------------------------------------------------------------- jars -- */

typedef struct {
  char *name;
  WebKitWebContext *context;
  GHashTable *snapshot;       /* key -> SoupCookie* (what both sides agree on) */
  GHashTable *recent_firefox; /* key -> double* time */
  gboolean sync_on, poll_running, poll_again, retired;
  int ops_inflight;
  GArray *flush_reqs;
} Jar;
static GHashTable *jars; /* name -> Jar* */
static GPtrArray *retired_jars;

static double now_secs(void) { return g_get_real_time() / 1e6; }

static char *cookie_key(SoupCookie *c) {
  return g_strdup_printf("%s\t%s\t%s", soup_cookie_get_domain(c), soup_cookie_get_path(c), soup_cookie_get_name(c));
}

static double cookie_expires(SoupCookie *c) {
  GDateTime *dt = soup_cookie_get_expires(c);
  return dt ? (double)g_date_time_to_unix(dt) : 0;
}

/* Equal for mirroring purposes. SameSite counts, so a SameSite-only change
   reaches the other engines too. */
static gboolean same_cookie(SoupCookie *a, SoupCookie *b) {
  gboolean sa = soup_cookie_get_expires(a) == NULL, sb = soup_cookie_get_expires(b) == NULL;
  return g_strcmp0(soup_cookie_get_value(a), soup_cookie_get_value(b)) == 0 &&
         soup_cookie_get_secure(a) == soup_cookie_get_secure(b) &&
         soup_cookie_get_http_only(a) == soup_cookie_get_http_only(b) && sa == sb &&
         soup_cookie_get_same_site_policy(a) == soup_cookie_get_same_site_policy(b) &&
         (sa || fabs(cookie_expires(a) - cookie_expires(b)) < 120);
}

static const char *same_site_name(SoupSameSitePolicy p) {
  switch (p) {
    case SOUP_SAME_SITE_POLICY_NONE: return "none";
    case SOUP_SAME_SITE_POLICY_LAX: return "lax";
    case SOUP_SAME_SITE_POLICY_STRICT: return "strict";
  }
  return "unset";
}

static SoupCookie *cookie_from_json(JsonObject *o) {
  const char *domain = json_object_get_string_member_with_default(o, "domain", "");
  const char *path = json_object_get_string_member_with_default(o, "path", "/");
  SoupCookie *c = soup_cookie_new(json_object_get_string_member_with_default(o, "name", ""),
                                  json_object_get_string_member_with_default(o, "value", ""), domain,
                                  *path ? path : "/", -1);
  soup_cookie_set_secure(c, json_object_get_boolean_member_with_default(o, "secure", FALSE));
  soup_cookie_set_http_only(c, json_object_get_boolean_member_with_default(o, "httpOnly", FALSE));
  if (!json_object_get_boolean_member_with_default(o, "session", TRUE)) {
    GDateTime *dt = g_date_time_new_from_unix_utc((gint64)json_object_get_double_member_with_default(o, "expires", 0));
    if (dt) {
      soup_cookie_set_expires(c, dt);
      g_date_time_unref(dt);
    }
  }
  const char *ss = json_object_get_string_member_with_default(o, "sameSite", "unset");
  if (!strcmp(ss, "none")) soup_cookie_set_same_site_policy(c, SOUP_SAME_SITE_POLICY_NONE);
  else if (!strcmp(ss, "strict")) soup_cookie_set_same_site_policy(c, SOUP_SAME_SITE_POLICY_STRICT);
  else soup_cookie_set_same_site_policy(c, SOUP_SAME_SITE_POLICY_LAX);
  return c;
}

static void emit_cookie(const char *ev, Jar *jar, SoupCookie *c) {
  JsonBuilder *b = event(ev, -1);
  add_str(b, "jar", jar->name);
  add_str(b, "domain", soup_cookie_get_domain(c));
  add_str(b, "path", soup_cookie_get_path(c));
  add_str(b, "name", soup_cookie_get_name(c));
  add_str(b, "value", soup_cookie_get_value(c));
  add_bool(b, "secure", soup_cookie_get_secure(c));
  add_bool(b, "httpOnly", soup_cookie_get_http_only(c));
  add_bool(b, "session", soup_cookie_get_expires(c) == NULL);
  add_double(b, "expires", cookie_expires(c));
  add_str(b, "sameSite", same_site_name(soup_cookie_get_same_site_policy(c)));
  emit(b);
}

static WebKitCookieManager *jar_cookies(Jar *jar) {
  return webkit_website_data_manager_get_cookie_manager(webkit_web_context_get_website_data_manager(jar->context));
}

static Jar *get_jar(const char *name) {
  Jar *jar = g_hash_table_lookup(jars, name);
  if (jar) return jar;
  jar = g_new0(Jar, 1);
  jar->name = g_strdup(name);
  /* Ephemeral: cookies, storage, cache and history stay in memory. */
  jar->context = webkit_web_context_new_ephemeral();
  webkit_cookie_manager_set_accept_policy(jar_cookies(jar), WEBKIT_COOKIE_POLICY_ACCEPT_NO_THIRD_PARTY);
  jar->snapshot = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, (GDestroyNotify)soup_cookie_free);
  jar->recent_firefox = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, g_free);
  jar->flush_reqs = g_array_new(FALSE, FALSE, sizeof(int));
  g_hash_table_insert(jars, g_strdup(name), jar);
  return jar;
}

static void mark_recent(Jar *jar, const char *key) {
  double *t = g_new(double, 1);
  *t = now_secs();
  g_hash_table_replace(jar->recent_firefox, g_strdup(key), t);
}

static gboolean recently_from_firefox(Jar *jar, const char *key, double now) {
  double *t = g_hash_table_lookup(jar->recent_firefox, key);
  return t && now - *t < 3;
}

typedef struct {
  Jar *jar;
  GCallback done; /* void (*)(gpointer) */
  gpointer data;
} OpCtx;

static void op_finished(GObject *src, GAsyncResult *res, gpointer user) {
  OpCtx *ctx = user;
  GError *err = NULL;
  /* add/delete share the boolean finish signature */
  webkit_cookie_manager_add_cookie_finish(WEBKIT_COOKIE_MANAGER(src), res, &err);
  if (err) g_error_free(err);
  ctx->jar->ops_inflight--;
  if (ctx->done) ((void (*)(gpointer))ctx->done)(ctx->data);
  g_free(ctx);
}

static void delete_finished(GObject *src, GAsyncResult *res, gpointer user) {
  OpCtx *ctx = user;
  GError *err = NULL;
  webkit_cookie_manager_delete_cookie_finish(WEBKIT_COOKIE_MANAGER(src), res, &err);
  if (err) g_error_free(err);
  ctx->jar->ops_inflight--;
  if (ctx->done) ((void (*)(gpointer))ctx->done)(ctx->data);
  g_free(ctx);
}

static void jar_apply(Jar *jar, SoupCookie *c, GCallback done, gpointer data) {
  char *key = cookie_key(c);
  mark_recent(jar, key);
  SoupCookie *have = g_hash_table_lookup(jar->snapshot, key);
  if (jar->retired || (have && same_cookie(have, c))) {
    g_free(key);
    soup_cookie_free(c);
    if (done) ((void (*)(gpointer))done)(data);
    return;
  }
  g_hash_table_replace(jar->snapshot, key, soup_cookie_copy(c));
  OpCtx *ctx = g_new0(OpCtx, 1);
  ctx->jar = jar;
  ctx->done = done;
  ctx->data = data;
  jar->ops_inflight++;
  webkit_cookie_manager_add_cookie(jar_cookies(jar), c, NULL, op_finished, ctx);
  soup_cookie_free(c);
}

static void jar_delete(Jar *jar, SoupCookie *c) {
  char *key = cookie_key(c);
  mark_recent(jar, key);
  if (jar->retired || !g_hash_table_remove(jar->snapshot, key)) {
    g_free(key);
    soup_cookie_free(c);
    return;
  }
  g_free(key);
  OpCtx *ctx = g_new0(OpCtx, 1);
  ctx->jar = jar;
  jar->ops_inflight++;
  webkit_cookie_manager_delete_cookie(jar_cookies(jar), c, NULL, delete_finished, ctx);
  soup_cookie_free(c);
}

static void jar_poll(Jar *jar);

/* Debugging and tests: the jar's cookies straight from WebKit's store (not
   from what the mirroring believes), as one "cookies" event. */
static void dump_finished(GObject *src, GAsyncResult *res, gpointer user) {
  char *name = user;
  GList *list = webkit_cookie_manager_get_all_cookies_finish(WEBKIT_COOKIE_MANAGER(src), res, NULL);
  JsonBuilder *b = event("cookies", -1);
  add_str(b, "jar", name);
  json_builder_set_member_name(b, "cookies");
  json_builder_begin_array(b);
  for (GList *l = list; l; l = l->next) {
    SoupCookie *c = l->data;
    json_builder_begin_object(b);
    add_str(b, "domain", soup_cookie_get_domain(c));
    add_str(b, "path", soup_cookie_get_path(c));
    add_str(b, "name", soup_cookie_get_name(c));
    add_str(b, "value", soup_cookie_get_value(c));
    add_str(b, "sameSite", same_site_name(soup_cookie_get_same_site_policy(c)));
    add_bool(b, "httpOnly", soup_cookie_get_http_only(c));
    json_builder_end_object(b);
  }
  json_builder_end_array(b);
  emit(b);
  g_list_free_full(list, (GDestroyNotify)soup_cookie_free);
  g_free(name);
}

static void poll_finished(GObject *src, GAsyncResult *res, gpointer user) {
  Jar *jar = user;
  GError *err = NULL;
  GList *list = webkit_cookie_manager_get_all_cookies_finish(WEBKIT_COOKIE_MANAGER(src), res, &err);
  if (err) g_error_free(err);
  jar->poll_running = FALSE;
  double now = now_secs();
  GHashTable *current = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, NULL);
  for (GList *l = list; l; l = l->next) g_hash_table_insert(current, cookie_key(l->data), l->data);
  if (jar->sync_on && !jar->retired) {
    GHashTableIter it;
    gpointer k, v;
    g_hash_table_iter_init(&it, current);
    while (g_hash_table_iter_next(&it, &k, &v)) {
      SoupCookie *have = g_hash_table_lookup(jar->snapshot, k);
      if (have && same_cookie(have, v)) continue;
      if (recently_from_firefox(jar, k, now)) continue; /* our own write landing */
      g_hash_table_replace(jar->snapshot, g_strdup(k), soup_cookie_copy(v));
      emit_cookie("cookie-set", jar, v);
    }
    GPtrArray *gone = g_ptr_array_new_with_free_func(g_free);
    g_hash_table_iter_init(&it, jar->snapshot);
    while (g_hash_table_iter_next(&it, &k, &v)) {
      if (g_hash_table_contains(current, k) || recently_from_firefox(jar, k, now)) continue;
      emit_cookie("cookie-del", jar, v);
      g_ptr_array_add(gone, g_strdup(k));
    }
    for (guint i = 0; i < gone->len; i++) g_hash_table_remove(jar->snapshot, g_ptr_array_index(gone, i));
    g_ptr_array_free(gone, TRUE);
  }
  g_hash_table_destroy(current);
  g_list_free_full(list, (GDestroyNotify)soup_cookie_free);
  for (guint i = 0; i < jar->flush_reqs->len; i++) {
    JsonBuilder *b = event("cookies-flushed", -1);
    add_str(b, "jar", jar->name);
    add_int(b, "req", g_array_index(jar->flush_reqs, int, i));
    emit(b);
  }
  g_array_set_size(jar->flush_reqs, 0);
  if (jar->poll_again) {
    jar->poll_again = FALSE;
    jar_poll(jar);
  }
}

static void jar_poll(Jar *jar) {
  if (jar->retired) return;
  if (jar->poll_running) {
    jar->poll_again = TRUE;
    return;
  }
  jar->poll_running = TRUE;
  webkit_cookie_manager_get_all_cookies(jar_cookies(jar), NULL, poll_finished, jar);
}

static gboolean poll_tick(gpointer unused) {
  if (g_quitting) return G_SOURCE_REMOVE;
  GHashTableIter it;
  gpointer k, v;
  g_hash_table_iter_init(&it, jars);
  while (g_hash_table_iter_next(&it, &k, &v))
    if (((Jar *)v)->sync_on) jar_poll(v);
  return G_SOURCE_CONTINUE;
}

typedef struct {
  Jar *jar;
  int remaining;
} ReplaceCtx;

static void replace_one_done(gpointer user) {
  ReplaceCtx *rc = user;
  if (--rc->remaining > 0) return;
  rc->jar->sync_on = TRUE;
  JsonBuilder *b = event("cookies-ready", -1);
  add_str(b, "jar", rc->jar->name);
  add_int(b, "count", g_hash_table_size(rc->jar->snapshot));
  emit(b);
  g_free(rc);
}

static void jar_replace(Jar *jar, JsonArray *arr) {
  /* The context is fresh and ephemeral at startup, so "replace" means load. */
  jar->sync_on = FALSE;
  g_hash_table_remove_all(jar->snapshot);
  guint n = arr ? json_array_get_length(arr) : 0;
  ReplaceCtx *rc = g_new0(ReplaceCtx, 1);
  rc->jar = jar;
  rc->remaining = n + 1; /* +1 so an empty list still finishes */
  for (guint i = 0; i < n; i++)
    jar_apply(jar, cookie_from_json(json_array_get_object_element(arr, i)), G_CALLBACK(replace_one_done), rc);
  replace_one_done(rc);
}

/* ----------------------------------------------------------------- tabs -- */

typedef struct {
  int id;
  int win;
  char *jar;
  int x, y, w, h;
  gboolean visible;
  Window container;
  GtkWidget *plug;
  WebKitWebView *view;
  double zoom; /* user zoom factor (Firefox's steps) */
} Tab;

static GHashTable *tabs; /* id -> Tab* */
static int focused_tab = -1;
static int next_popup_id = 2000000;

static Win *win_for(Tab *t) { return g_hash_table_lookup(wins, GINT_TO_POINTER(t->win)); }

static Window create_container(Tab *t) {
  Win *win = win_for(t);
  if (!win) return 0;
  int scr = DefaultScreen(dpy);
  Visual *vis = DefaultVisual(dpy, scr);
  XSetWindowAttributes a = {0};
  a.colormap = XCreateColormap(dpy, win->parent, vis, AllocNone);
  a.background_pixel = WhitePixel(dpy, scr);
  a.border_pixel = 0;
  Window c = XCreateWindow(dpy, win->parent, t->x + win->off_x, t->y + win->off_y, MAX(1, t->w), MAX(1, t->h), 0,
                           DefaultDepth(dpy, scr), InputOutput, vis, CWColormap | CWBackPixel | CWBorderPixel, &a);
  XFlush(dpy);
  return c;
}

static void apply_zoom(Tab *t) {
  /* WebKitGTK only supports integer device scales; cover the rest with zoom
     so pages match Firefox's size. */
  int gdk_scale = gdk_window_get_scale_factor(gtk_widget_get_window(t->plug)) ?: 1;
  webkit_web_view_set_zoom_level(t->view, t->zoom * g_scale / gdk_scale);
}

static void apply_geometry(Tab *t) {
  Win *win = win_for(t);
  if (!win || !t->container) return;
  int w = MAX(1, t->w), h = MAX(1, t->h);
  XMoveResizeWindow(dpy, t->container, t->x + win->off_x, t->y + win->off_y, w, h);
  GdkWindow *gw = gtk_widget_get_window(t->plug);
  if (gw) {
    XMoveResizeWindow(dpy, GDK_WINDOW_XID(gw), 0, 0, w, h);
    /* Our container isn't an XEMBED socket, so nobody else maps the plug. */
    XMapWindow(dpy, GDK_WINDOW_XID(gw));
  }
  int gs = gw ? (gdk_window_get_scale_factor(gw) ?: 1) : 1;
  gtk_window_resize(GTK_WINDOW(t->plug), MAX(1, w / gs), MAX(1, h / gs));
  if (t->visible) XMapRaised(dpy, t->container);
  else XUnmapWindow(dpy, t->container);
  XFlush(dpy);
}

static void focus_firefox(int win_id) {
  focused_tab = -1;
  Win *win = g_hash_table_lookup(wins, GINT_TO_POINTER(win_id));
  if (!win) return;
  XSetInputFocus(dpy, win->focus_target ? win->focus_target : win->parent, RevertToParent, CurrentTime);
  XFlush(dpy);
}

static void focus_tab(Tab *t) {
  if (!t->visible) return;
  focused_tab = t->id;
  GdkWindow *gw = gtk_widget_get_window(t->plug);
  if (gw) {
    XSetInputFocus(dpy, GDK_WINDOW_XID(gw), RevertToParent, CurrentTime);
    XFlush(dpy);
  }
  gtk_widget_grab_focus(GTK_WIDGET(t->view));
}

static unsigned int last_buttons = 0;

static gboolean poll_pointer(gpointer unused) {
  if (g_quitting) return G_SOURCE_REMOVE;
  if (g_hash_table_size(wins) == 0) return G_SOURCE_CONTINUE;
  Window root = DefaultRootWindow(dpy), root_ret, top;
  int rx, ry, wx, wy;
  unsigned int mask = 0;
  if (!XQueryPointer(dpy, root, &root_ret, &top, &rx, &ry, &wx, &wy, &mask)) return G_SOURCE_CONTINUE;
  unsigned int buttons = mask & (Button1Mask | Button2Mask | Button3Mask);
  if (buttons && !last_buttons && top) {
    GHashTableIter it;
    gpointer k, v;
    g_hash_table_iter_init(&it, wins);
    while (g_hash_table_iter_next(&it, &k, &v)) {
      Win *win = v;
      if (top_ancestor(win->parent) != top) continue;
      int px = 0, py = 0;
      Window dummy;
      XTranslateCoordinates(dpy, root, win->parent, rx, ry, &px, &py, &dummy);
      int lx = px - win->off_x, ly = py - win->off_y;
      Tab *hit = NULL;
      GHashTableIter ti;
      gpointer tk, tv;
      g_hash_table_iter_init(&ti, tabs);
      while (g_hash_table_iter_next(&ti, &tk, &tv)) {
        Tab *t = tv;
        if (t->win != win->id || !t->visible) continue;
        if (lx >= t->x && ly >= t->y && lx < t->x + t->w && ly < t->y + t->h) {
          hit = t;
          break;
        }
      }
      if (hit && hit->id != focused_tab) focus_tab(hit);
      else if (!hit && focused_tab >= 0) focus_firefox(win->id);
      break;
    }
  }
  last_buttons = buttons;
  return G_SOURCE_CONTINUE;
}

/* --------------------------------------------------- privacy (navigation) -- */
/* Page loads wait for Firefox's Safe Browsing verdict, like Chromium tabs. */

typedef struct {
  int tab;
  char *url;
  WebKitPolicyDecision *decision;
} Check;
static GHashTable *checks; /* req -> Check* */
static int next_check = 1;

static void resolve_check(int req, const char *reason) {
  Check *c = g_hash_table_lookup(checks, GINT_TO_POINTER(req));
  if (!c) return;
  if (reason && *reason) {
    webkit_policy_decision_ignore(c->decision);
    JsonBuilder *b = event("unsafe", c->tab);
    add_str(b, "url", c->url);
    add_str(b, "kind", "navigation");
    add_str(b, "reason", reason);
    emit(b);
  } else {
    webkit_policy_decision_use(c->decision);
  }
  g_object_unref(c->decision);
  g_free(c->url);
  g_hash_table_remove(checks, GINT_TO_POINTER(req));
}

/* Firefox didn't answer in time. This host only checks page loads, and a
   stalled check must never make browsing impossible, so the load goes ahead
   (Chromium tabs do the same for page loads; see chromium_host.cc). */
static gboolean check_timeout(gpointer req) {
  Check *c = g_hash_table_lookup(checks, req);
  if (c) {
    JsonBuilder *b = event("check-timeout", c->tab);
    add_str(b, "url", c->url);
    add_str(b, "kind", "navigation");
    add_bool(b, "allowed", TRUE);
    emit(b);
    char *msg = g_strdup_printf("privacy check timed out, allowed: %s", c->url);
    log_msg(msg);
    g_free(msg);
    resolve_check(GPOINTER_TO_INT(req), "");
  }
  return G_SOURCE_REMOVE;
}

/* --------------------------------------------------------------- dialogs -- */

static GHashTable *dialogs; /* req -> WebKitScriptDialog* */
static int next_dialog = 1;

/* ----------------------------------------------------------- tab events -- */

static Tab *tab_for_view(WebKitWebView *v) { return g_object_get_data(G_OBJECT(v), "hybrid-tab"); }

static void on_title(WebKitWebView *v, GParamSpec *p, gpointer u) {
  Tab *t = tab_for_view(v);
  if (!t) return;
  JsonBuilder *b = event("title", t->id);
  add_str(b, "title", webkit_web_view_get_title(v));
  emit(b);
}

static void on_uri(WebKitWebView *v, GParamSpec *p, gpointer u) {
  Tab *t = tab_for_view(v);
  if (!t) return;
  JsonBuilder *b = event("url", t->id);
  add_str(b, "url", webkit_web_view_get_uri(v));
  emit(b);
}

static void emit_loading(Tab *t, gboolean loading) {
  JsonBuilder *b = event("loading", t->id);
  add_bool(b, "loading", loading);
  add_bool(b, "canGoBack", webkit_web_view_can_go_back(t->view));
  add_bool(b, "canGoForward", webkit_web_view_can_go_forward(t->view));
  emit(b);
}

static void on_load_changed(WebKitWebView *v, WebKitLoadEvent ev, gpointer u) {
  Tab *t = tab_for_view(v);
  if (!t) return;
  if (ev == WEBKIT_LOAD_STARTED) emit_loading(t, TRUE);
  if (ev == WEBKIT_LOAD_FINISHED) {
    emit_loading(t, FALSE);
    /* WebKitGTK doesn't expose the page's icon URL; use the site's default. */
    const char *uri = webkit_web_view_get_uri(v);
    GUri *g = uri ? g_uri_parse(uri, G_URI_FLAGS_NONE, NULL) : NULL;
    if (g && g_uri_get_host(g) && (!g_strcmp0(g_uri_get_scheme(g), "https") || !g_strcmp0(g_uri_get_scheme(g), "http"))) {
      char *fav = g_uri_get_port(g) > 0
                      ? g_strdup_printf("%s://%s:%d/favicon.ico", g_uri_get_scheme(g), g_uri_get_host(g), g_uri_get_port(g))
                      : g_strdup_printf("%s://%s/favicon.ico", g_uri_get_scheme(g), g_uri_get_host(g));
      JsonBuilder *b = event("favicon", t->id);
      add_str(b, "url", fav);
      emit(b);
      g_free(fav);
    }
    if (g) g_uri_unref(g);
  }
}

static gboolean on_decide_policy(WebKitWebView *v, WebKitPolicyDecision *d, WebKitPolicyDecisionType type, gpointer u) {
  Tab *t = tab_for_view(v);
  if (!t || type != WEBKIT_POLICY_DECISION_TYPE_NAVIGATION_ACTION || getenv("HYBRID_NO_PROTECTION")) return FALSE;
  WebKitNavigationAction *a = webkit_navigation_policy_decision_get_navigation_action(WEBKIT_NAVIGATION_POLICY_DECISION(d));
  const char *uri = webkit_uri_request_get_uri(webkit_navigation_action_get_request(a));
  if (!uri || !(g_str_has_prefix(uri, "http://") || g_str_has_prefix(uri, "https://"))) return FALSE;
  int req = next_check++;
  Check *c = g_new0(Check, 1);
  c->tab = t->id;
  c->url = g_strdup(uri);
  c->decision = g_object_ref(d);
  g_hash_table_insert(checks, GINT_TO_POINTER(req), c);
  JsonBuilder *b = event("classify", t->id);
  add_int(b, "req", req);
  add_str(b, "kind", "navigation");
  add_str(b, "url", uri);
  add_str(b, "top", "");
  add_str(b, "jar", t->jar);
  emit(b);
  const char *ms = getenv("HYBRID_CHECK_TIMEOUT_MS");
  g_timeout_add(ms ? MAX(50, atoi(ms)) : 1500, check_timeout, GINT_TO_POINTER(req));
  return TRUE;
}

static gboolean on_script_dialog(WebKitWebView *v, WebKitScriptDialog *dialog, gpointer u) {
  Tab *t = tab_for_view(v);
  if (!t) return FALSE;
  int req = next_dialog++;
  g_hash_table_insert(dialogs, GINT_TO_POINTER(req), webkit_script_dialog_ref(dialog));
  const char *type = "alert";
  switch (webkit_script_dialog_get_dialog_type(dialog)) {
    case WEBKIT_SCRIPT_DIALOG_CONFIRM: type = "confirm"; break;
    case WEBKIT_SCRIPT_DIALOG_PROMPT: type = "prompt"; break;
    case WEBKIT_SCRIPT_DIALOG_BEFORE_UNLOAD_CONFIRM: type = "beforeunload"; break;
    default: break;
  }
  JsonBuilder *b = event("dialog", t->id);
  add_int(b, "req", req);
  add_str(b, "type", type);
  add_str(b, "message", webkit_script_dialog_get_message(dialog));
  add_str(b, "defaultText", webkit_script_dialog_get_dialog_type(dialog) == WEBKIT_SCRIPT_DIALOG_PROMPT
                                ? webkit_script_dialog_prompt_get_default_text(dialog) : "");
  add_str(b, "origin", webkit_web_view_get_uri(v));
  emit(b);
  return TRUE;
}

static gboolean on_key(GtkWidget *w, GdkEventKey *e, gpointer u) {
  Tab *t = tab_for_view(WEBKIT_WEB_VIEW(w));
  if (!t) return FALSE;
  gboolean ctrl = e->state & GDK_CONTROL_MASK, shift = e->state & GDK_SHIFT_MASK, alt = e->state & GDK_MOD1_MASK;
  guint k = gdk_keyval_to_lower(e->keyval);
  const char *key = NULL;
  if (ctrl && k == GDK_KEY_t) key = shift ? "ctrl+shift+t" : "ctrl+t";
  else if (ctrl && k == GDK_KEY_w) key = "ctrl+w";
  else if (ctrl && k == GDK_KEY_l) key = "ctrl+l";
  else if (ctrl && k == GDK_KEY_n) key = shift ? "ctrl+shift+n" : "ctrl+n";
  else if (ctrl && shift && k == GDK_KEY_p) key = "ctrl+shift+p";
  else if (ctrl && (k == GDK_KEY_Tab || k == GDK_KEY_ISO_Left_Tab)) key = shift ? "ctrl+shift+tab" : "ctrl+tab";
  else if (ctrl && k == GDK_KEY_Page_Up) key = "ctrl+pageup";
  else if (ctrl && k == GDK_KEY_Page_Down) key = "ctrl+pagedown";
  else if (ctrl && alt && k == GDK_KEY_e) key = "ctrl+alt+e";
  else if (k == GDK_KEY_F6) key = "f6";
  else if (k == GDK_KEY_F5) key = shift ? "shift+f5" : "f5";
  else if (ctrl && k == GDK_KEY_r) key = shift ? "ctrl+shift+r" : "ctrl+r";
  else if (ctrl && (k == GDK_KEY_equal || k == GDK_KEY_plus || k == GDK_KEY_KP_Add)) key = "zoom-in";
  else if (ctrl && (k == GDK_KEY_minus || k == GDK_KEY_KP_Subtract)) key = "zoom-out";
  else if (ctrl && (k == GDK_KEY_0 || k == GDK_KEY_KP_0)) key = "zoom-reset";
  if (!key) return FALSE;
  JsonBuilder *b = event("shortcut", t->id);
  add_str(b, "key", key);
  emit(b);
  return TRUE;
}

static void destroy_tab(Tab *t, gboolean notify) {
  if (focused_tab == t->id) focused_tab = -1;
  int id = t->id;
  if (t->plug) gtk_widget_destroy(t->plug);
  if (t->container) {
    XDestroyWindow(dpy, t->container);
    XFlush(dpy);
  }
  g_free(t->jar);
  g_hash_table_remove(tabs, GINT_TO_POINTER(id));
  g_free(t);
  if (notify) emit(event("closed", id));
  if (g_quitting && g_hash_table_size(tabs) == 0) gtk_main_quit();
}

static void on_close(WebKitWebView *v, gpointer u) {
  Tab *t = tab_for_view(v);
  if (t) destroy_tab(t, TRUE);
}

static GtkWidget *on_create(WebKitWebView *v, WebKitNavigationAction *a, gpointer u);

static void wire_view(Tab *t) {
  g_object_set_data(G_OBJECT(t->view), "hybrid-tab", t);
  WebKitSettings *s = webkit_web_view_get_settings(t->view);
  webkit_settings_set_enable_smooth_scrolling(s, TRUE);
  webkit_settings_set_javascript_can_open_windows_automatically(s, FALSE);
  g_signal_connect(t->view, "notify::title", G_CALLBACK(on_title), NULL);
  g_signal_connect(t->view, "notify::uri", G_CALLBACK(on_uri), NULL);
  g_signal_connect(t->view, "load-changed", G_CALLBACK(on_load_changed), NULL);
  g_signal_connect(t->view, "decide-policy", G_CALLBACK(on_decide_policy), NULL);
  g_signal_connect(t->view, "script-dialog", G_CALLBACK(on_script_dialog), NULL);
  g_signal_connect(t->view, "key-press-event", G_CALLBACK(on_key), NULL);
  g_signal_connect(t->view, "close", G_CALLBACK(on_close), NULL);
  g_signal_connect(t->view, "create", G_CALLBACK(on_create), NULL);
}

static void place_tab(Tab *t) {
  t->container = create_container(t);
  t->plug = gtk_plug_new(t->container);
  gtk_container_add(GTK_CONTAINER(t->plug), GTK_WIDGET(t->view));
  gtk_widget_show_all(t->plug);
  apply_zoom(t);
  apply_geometry(t);
}

static void on_ready_to_show(WebKitWebView *v, gpointer u) {
  Tab *t = tab_for_view(v);
  if (t) apply_geometry(t);
}

/* A page opened a popup: keep it (window.opener works) and host it as a new
   tab in the opener's window. */
static GtkWidget *on_create(WebKitWebView *opener_view, WebKitNavigationAction *a, gpointer u) {
  Tab *opener = tab_for_view(opener_view);
  if (!opener) return NULL;
  Tab *t = g_new0(Tab, 1);
  t->id = next_popup_id++;
  t->win = opener->win;
  t->jar = g_strdup(opener->jar);
  t->x = opener->x;
  t->y = opener->y;
  t->w = opener->w;
  t->h = opener->h;
  t->zoom = 1;
  t->visible = FALSE;
  t->view = WEBKIT_WEB_VIEW(webkit_web_view_new_with_related_view(opener_view));
  g_hash_table_insert(tabs, GINT_TO_POINTER(t->id), t);
  wire_view(t);
  g_signal_connect(t->view, "ready-to-show", G_CALLBACK(on_ready_to_show), NULL);
  place_tab(t);
  const char *uri = webkit_uri_request_get_uri(webkit_navigation_action_get_request(a));
  JsonBuilder *b = event("popup", t->id);
  add_int(b, "opener", opener->id);
  add_str(b, "url", uri);
  add_bool(b, "background", FALSE);
  emit(b);
  return GTK_WIDGET(t->view);
}

/* ------------------------------------------------------------- commands -- */

static void handle_line(const char *line);

typedef struct {
  int win;
  char *marker;
  int attempt;
} AttachCtx;

static gboolean try_attach(gpointer user) {
  AttachCtx *ac = user;
  Window w = find_by_title(DefaultRootWindow(dpy), ac->marker, 0);
  if (!w) {
    if (++ac->attempt < 60) return G_SOURCE_CONTINUE;
    JsonBuilder *b = event("error", -1);
    add_int(b, "win", ac->win);
    add_str(b, "msg", "Firefox window not found. Is Firefox running on X11 (MOZ_ENABLE_WAYLAND=0)?");
    emit(b);
  } else {
    Win *win = g_new0(Win, 1);
    win->id = ac->win;
    win->parent = w;
    compute_layout(win);
    g_hash_table_replace(wins, GINT_TO_POINTER(ac->win), win);
    JsonBuilder *b = event("attached", -1);
    add_int(b, "win", ac->win);
    add_double(b, "xid", (double)w);
    add_int(b, "offsetX", win->off_x);
    add_int(b, "offsetY", win->off_y);
    add_bool(b, "focusProxy", win->focus_target != 0);
    emit(b);
  }
  g_free(ac->marker);
  g_free(ac);
  return G_SOURCE_REMOVE;
}

static void read_rect(JsonObject *c, Tab *t) {
  if (json_object_has_member(c, "x")) t->x = json_object_get_int_member(c, "x");
  if (json_object_has_member(c, "y")) t->y = json_object_get_int_member(c, "y");
  if (json_object_has_member(c, "w")) t->w = json_object_get_int_member(c, "w");
  if (json_object_has_member(c, "h")) t->h = json_object_get_int_member(c, "h");
}

static const char *jar_name(JsonObject *c) { return json_object_get_string_member_with_default(c, "jar", "normal"); }

static int cookie_ops_inflight(void) {
  int n = 0;
  GHashTableIter it;
  gpointer k, v;
  g_hash_table_iter_init(&it, jars);
  while (g_hash_table_iter_next(&it, &k, &v)) n += ((Jar *)v)->ops_inflight;
  return n;
}

static gboolean retry_line(gpointer line) {
  handle_line(line);
  g_free(line);
  return G_SOURCE_REMOVE;
}

static void handle_line(const char *line) {
  JsonParser *p = json_parser_new();
  if (!json_parser_load_from_data(p, line, -1, NULL) || !JSON_NODE_HOLDS_OBJECT(json_parser_get_root(p))) {
    log_msg("bad command");
    g_object_unref(p);
    return;
  }
  JsonObject *c = json_node_get_object(json_parser_get_root(p));
  const char *cmd = json_object_get_string_member_with_default(c, "cmd", "");
  int id = json_object_has_member(c, "id") ? json_object_get_int_member(c, "id") : -1;
  Tab *t = id >= 0 ? g_hash_table_lookup(tabs, GINT_TO_POINTER(id)) : NULL;

  if (!strcmp(cmd, "attach")) {
    AttachCtx *ac = g_new0(AttachCtx, 1);
    ac->win = json_object_get_int_member(c, "win");
    ac->marker = g_strdup(json_object_get_string_member(c, "marker"));
    g_timeout_add(50, try_attach, ac);
  } else if (!strcmp(cmd, "detach")) {
    int win_id = json_object_get_int_member(c, "win");
    GList *vals = g_hash_table_get_values(tabs);
    for (GList *l = vals; l; l = l->next)
      if (((Tab *)l->data)->win == win_id) destroy_tab(l->data, TRUE);
    g_list_free(vals);
    g_hash_table_remove(wins, GINT_TO_POINTER(win_id));
  } else if (!strcmp(cmd, "quit")) {
    g_quitting = TRUE;
    GList *vals = g_hash_table_get_values(tabs);
    for (GList *l = vals; l; l = l->next) destroy_tab(l->data, TRUE);
    g_list_free(vals);
    gtk_main_quit();
  } else if (!strcmp(cmd, "unfocus")) {
    focus_firefox(json_object_get_int_member_with_default(c, "win", -1));
  } else if (!strcmp(cmd, "cookies-replace")) {
    jar_replace(get_jar(jar_name(c)), json_object_get_array_member(c, "cookies"));
  } else if (!strcmp(cmd, "cookie-set")) {
    jar_apply(get_jar(jar_name(c)), cookie_from_json(c), NULL, NULL);
  } else if (!strcmp(cmd, "cookie-del")) {
    jar_delete(get_jar(jar_name(c)), cookie_from_json(c));
  } else if (!strcmp(cmd, "cookies-flush")) {
    Jar *jar = get_jar(jar_name(c));
    int req = json_object_get_int_member(c, "req");
    g_array_append_val(jar->flush_reqs, req);
    jar_poll(jar);
  } else if (!strcmp(cmd, "cookies-dump")) {
    Jar *jar = get_jar(jar_name(c));
    webkit_cookie_manager_get_all_cookies(jar_cookies(jar), NULL, dump_finished, g_strdup(jar->name));
  } else if (!strcmp(cmd, "private-end")) {
    GList *vals = g_hash_table_get_values(tabs);
    for (GList *l = vals; l; l = l->next)
      if (!g_strcmp0(((Tab *)l->data)->jar, "private")) destroy_tab(l->data, TRUE);
    g_list_free(vals);
    Jar *pj = g_hash_table_lookup(jars, "private");
    if (pj) {
      pj->retired = TRUE;
      pj->sync_on = FALSE;
      g_hash_table_steal(jars, "private");
      g_ptr_array_add(retired_jars, pj);
      g_clear_object(&pj->context); /* drop the in-memory profile */
    }
    emit(event("private-ended", -1));
  } else if (!strcmp(cmd, "classified")) {
    resolve_check(json_object_get_int_member(c, "req"), json_object_get_string_member_with_default(c, "reason", ""));
  } else if (!strcmp(cmd, "protection-reset")) {
    /* no per-request cache in this host */
  } else if (!strcmp(cmd, "dialog-reply")) {
    int req = json_object_get_int_member(c, "req");
    WebKitScriptDialog *d = g_hash_table_lookup(dialogs, GINT_TO_POINTER(req));
    if (d) {
      gboolean ok = json_object_get_boolean_member_with_default(c, "ok", FALSE);
      WebKitScriptDialogType type = webkit_script_dialog_get_dialog_type(d);
      if (type == WEBKIT_SCRIPT_DIALOG_CONFIRM || type == WEBKIT_SCRIPT_DIALOG_BEFORE_UNLOAD_CONFIRM)
        webkit_script_dialog_confirm_set_confirmed(d, ok);
      else if (type == WEBKIT_SCRIPT_DIALOG_PROMPT)
        webkit_script_dialog_prompt_set_text(d, ok ? json_object_get_string_member_with_default(c, "text", "") : NULL);
      g_hash_table_steal(dialogs, GINT_TO_POINTER(req));
      webkit_script_dialog_close(d);
      webkit_script_dialog_unref(d);
    }
  } else if (!strcmp(cmd, "create")) {
    if (cookie_ops_inflight() > 0) {
      /* Let pending cookie writes land so the first request carries them. */
      g_timeout_add(20, retry_line, g_strdup(line));
    } else {
      int win_id = json_object_get_int_member(c, "win");
      if (!g_hash_table_contains(wins, GINT_TO_POINTER(win_id))) {
        log_msg("create for unattached window");
      } else {
        t = g_new0(Tab, 1);
        t->id = id;
        t->win = win_id;
        t->jar = g_strdup(jar_name(c));
        t->zoom = 1;
        read_rect(c, t);
        t->visible = json_object_get_boolean_member_with_default(c, "visible", TRUE);
        Jar *jar = get_jar(t->jar);
        t->view = WEBKIT_WEB_VIEW(webkit_web_view_new_with_context(jar->context));
        g_hash_table_insert(tabs, GINT_TO_POINTER(id), t);
        wire_view(t);
        place_tab(t);
        webkit_web_view_load_uri(t->view, json_object_get_string_member(c, "url"));
        if (t->visible) focus_tab(t);
        emit(event("created", id));
      }
    }
  } else if (!t) {
    if (id >= 0) log_msg("unknown tab");
  } else if (!strcmp(cmd, "bounds")) {
    read_rect(c, t);
    apply_geometry(t);
  } else if (!strcmp(cmd, "show")) {
    read_rect(c, t);
    t->visible = TRUE;
    apply_geometry(t);
  } else if (!strcmp(cmd, "hide")) {
    t->visible = FALSE;
    if (focused_tab == id) focus_firefox(t->win);
    apply_geometry(t);
  } else if (!strcmp(cmd, "close")) {
    if (focused_tab == id) focus_firefox(t->win);
    destroy_tab(t, TRUE);
  } else if (!strcmp(cmd, "navigate")) {
    webkit_web_view_load_uri(t->view, json_object_get_string_member(c, "url"));
  } else if (!strcmp(cmd, "back")) {
    webkit_web_view_go_back(t->view);
  } else if (!strcmp(cmd, "forward")) {
    webkit_web_view_go_forward(t->view);
  } else if (!strcmp(cmd, "reload")) {
    if (json_object_get_boolean_member_with_default(c, "skipCache", FALSE)) webkit_web_view_reload_bypass_cache(t->view);
    else webkit_web_view_reload(t->view);
  } else if (!strcmp(cmd, "stop")) {
    webkit_web_view_stop_loading(t->view);
  } else if (!strcmp(cmd, "zoom")) {
    /* The shell sends Chromium-style levels: factor = 1.2 ^ level. */
    t->zoom = pow(1.2, json_object_get_double_member(c, "level"));
    apply_zoom(t);
  } else if (!strcmp(cmd, "focus")) {
    focus_tab(t);
  } else {
    log_msg("unknown command");
  }
  g_object_unref(p);
}

static GString *inbuf;

static gboolean on_stdin(GIOChannel *ch, GIOCondition cond, gpointer u) {
  char buf[65536];
  gsize n = 0;
  GIOStatus st = g_io_channel_read_chars(ch, buf, sizeof buf, &n, NULL);
  if (n > 0) {
    g_string_append_len(inbuf, buf, n);
    char *nl;
    while ((nl = memchr(inbuf->str, '\n', inbuf->len))) {
      char *line = g_strndup(inbuf->str, nl - inbuf->str);
      g_string_erase(inbuf, 0, nl - inbuf->str + 1);
      if (*line) handle_line(line);
      g_free(line);
    }
  }
  if (st == G_IO_STATUS_EOF || (cond & (G_IO_HUP | G_IO_ERR))) {
    /* Firefox went away: shut down with it. */
    handle_line("{\"cmd\":\"quit\"}");
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;
}

static int x_error(Display *d, XErrorEvent *e) { return 0; }

int main(int argc, char **argv) {
  /* Windowed embedding needs X11; under Wayland this runs on XWayland. */
  g_setenv("GDK_BACKEND", "x11", TRUE);
  const char *scale = getenv("HYBRID_SCALE");
  if (scale && atof(scale) >= 0.5 && atof(scale) <= 8) g_scale = atof(scale);
  if (g_scale >= 2 && !getenv("GDK_SCALE")) {
    char s[8];
    snprintf(s, sizeof s, "%d", (int)g_scale);
    g_setenv("GDK_SCALE", s, TRUE);
  }
  gtk_init(&argc, &argv);
  dpy = gdk_x11_get_default_xdisplay();
  XSetErrorHandler(x_error);
  wins = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, g_free);
  tabs = g_hash_table_new(g_direct_hash, g_direct_equal);
  jars = g_hash_table_new(g_str_hash, g_str_equal);
  retired_jars = g_ptr_array_new();
  checks = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, g_free);
  dialogs = g_hash_table_new(g_direct_hash, g_direct_equal);
  inbuf = g_string_new(NULL);

  GIOChannel *in = g_io_channel_unix_new(0);
  g_io_channel_set_encoding(in, NULL, NULL);
  g_io_channel_set_flags(in, G_IO_FLAG_NONBLOCK, NULL);
  g_io_add_watch(in, G_IO_IN | G_IO_HUP | G_IO_ERR, on_stdin, NULL);
  g_timeout_add(1000, poll_tick, NULL);
  g_timeout_add(30, poll_pointer, NULL);
  emit(event("ready", -1));
  gtk_main();
  fflush(stdout);
  return 0;
}
