/* Firefox's protections, applied to Chromium tabs.
 *
 * The engine asks before every page load and every third-party request a
 * Chromium page makes. Firefox answers using its own machinery:
 *  - Safe Browsing (phishing, malware, unwanted software) for page loads,
 *    following browser.safebrowsing.* settings;
 *  - Enhanced Tracking Protection for third-party requests, following the
 *    user's ETP level (Standard/Strict/Custom), its separate private-window
 *    setting, per-site exceptions, and Firefox's entity list (which lets a
 *    site load its own related domains).
 * Answers are reasons ("tracking", "phishing", ...) or "" to allow.
 */

const classifier = Cc["@mozilla.org/url-classifier/dbservice;1"].getService(Ci.nsIURIClassifier);
const log = (...a) => console.log("[hybrid]", ...a);

const bool = (name, fallback = false) => Services.prefs.getBoolPref(name, fallback);

// Tracking protection features and the prefs that switch them on, mirroring
// what Gecko applies to its own tabs.
function trackingFeatures(isPrivate) {
  const wanted = [];
  const trackingContent = isPrivate
    ? bool("privacy.trackingprotection.pbmode.enabled", true) || bool("privacy.trackingprotection.enabled")
    : bool("privacy.trackingprotection.enabled");
  if (trackingContent) wanted.push(["tracking-protection", "tracking"]);
  if (bool("privacy.trackingprotection.socialtracking.enabled")) wanted.push(["socialtracking-protection", "social tracking"]);
  if (bool("privacy.trackingprotection.cryptomining.enabled")) wanted.push(["cryptomining-protection", "cryptomining"]);
  if (bool("privacy.trackingprotection.fingerprinting.enabled")) wanted.push(["fingerprinting-protection", "fingerprinting"]);
  if (bool("privacy.trackingprotection.emailtracking.enabled") ||
      (isPrivate && bool("privacy.trackingprotection.emailtracking.pbmode.enabled"))) {
    wanted.push(["emailtracking-protection", "email tracking"]);
  }
  return featuresFor(wanted);
}

function safeBrowsingFeatures() {
  const wanted = [];
  if (bool("browser.safebrowsing.phishing.enabled", true)) wanted.push(["phishing", "phishing"]);
  if (bool("browser.safebrowsing.malware.enabled", true)) {
    wanted.push(["malware", "malware"]);
    wanted.push(["harmful", "harmful"]);
    if (bool("browser.safebrowsing.downloads.remote.block_potentially_unwanted", true)) {
      wanted.push(["unwanted", "unwanted software"]);
    }
  }
  return featuresFor(wanted);
}

const featureCache = new Map();
function featuresFor(pairs) {
  const out = [];
  for (const [name, reason] of pairs) {
    if (!featureCache.has(name)) {
      let f = null;
      try {
        f = classifier.getFeatureByName(name);
      } catch (e) {}
      featureCache.set(name, f);
      if (!f) log("classifier feature not available in this Firefox:", name);
    }
    const f = featureCache.get(name);
    if (f) out.push({ feature: f, reason, name });
  }
  return out;
}

function lookup(uri, features, listType) {
  if (!features.length) return Promise.resolve([]);
  return new Promise(resolve => {
    try {
      classifier.asyncClassifyLocalWithFeatures(uri, features.map(f => f.feature), listType, results => {
        resolve(results || []);
      });
    } catch (e) {
      resolve([]);
    }
  });
}

function reasonFor(results, features) {
  for (const r of results) {
    const name = r.feature?.name;
    const match = features.find(f => f.name === name);
    if (match) return match.reason;
  }
  return results.length ? features[0]?.reason || "blocked" : "";
}

function baseDomain(uri) {
  try {
    return Services.eTLD.getBaseDomain(uri);
  } catch (e) {
    return uri.host;
  }
}

// The user turned protection off for this site (shield icon -> toggle).
function hasException(topURI, isPrivate) {
  try {
    const principal = Services.scriptSecurityManager.createContentPrincipal(
      topURI,
      isPrivate ? { privateBrowsingId: 1 } : {}
    );
    for (const type of isPrivate ? ["trackingprotection-pb", "trackingprotection"] : ["trackingprotection"]) {
      if (Services.perms.testExactPermissionFromPrincipal(principal, type) === Services.perms.ALLOW_ACTION) {
        return true;
      }
    }
  } catch (e) {}
  return false;
}

export const HybridPrivacy = {
  async classify({ kind, url, top, jar }) {
    let uri;
    try {
      uri = Services.io.newURI(url);
    } catch (e) {
      return "";
    }
    const isPrivate = jar === "private";

    if (kind === "navigation") {
      const features = safeBrowsingFeatures();
      const results = await lookup(uri, features, Ci.nsIUrlClassifierFeature.blocklist);
      return results.length ? reasonFor(results, features) : "";
    }

    let topURI;
    try {
      topURI = Services.io.newURI(top);
    } catch (e) {
      return "";
    }
    // First-party requests are never tracking.
    if (baseDomain(uri) === baseDomain(topURI)) return "";
    if (hasException(topURI, isPrivate)) return "";

    const features = trackingFeatures(isPrivate);
    const results = await lookup(uri, features, Ci.nsIUrlClassifierFeature.blocklist);
    if (!results.length) return "";

    // Firefox's entity list allows a site's own related domains.
    const entityURI = Services.io.newURI(`${topURI.prePath}/?resource=${uri.host}`);
    const allowed = await lookup(entityURI, features, Ci.nsIUrlClassifierFeature.entitylist);
    if (allowed.length) return "";
    return reasonFor(results, features);
  },

  // Calls |onChange| when settings or per-site exceptions that affect
  // verdicts change, so cached answers can be dropped.
  watch(onChange) {
    const prefObserver = { observe: () => onChange() };
    for (const branch of ["privacy.trackingprotection.", "browser.safebrowsing.", "browser.contentblocking."]) {
      Services.prefs.addObserver(branch, prefObserver);
    }
    Services.obs.addObserver(
      {
        observe(subject) {
          try {
            const perm = subject.QueryInterface(Ci.nsIPermission);
            if (perm.type.startsWith("trackingprotection")) onChange();
          } catch (e) {
            onChange();
          }
        },
      },
      "perm-changed"
    );
  },
};
