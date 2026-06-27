// Shared client-side loader for SKALA pages: fetch the changing data + images from
// Cloudflare R2 (instant) instead of waiting on GitHub Pages' build + 10-min CDN
// cache. R2 mirrors docs/, so an asset's R2 key == its docs-relative path.
//
//   SKALA_R2.url(path)              -> cache-busted URL (R2 if BASE set, else Pages)
//   SKALA_R2.loadJSON(name)        -> Promise<json>, R2 first then the Pages copy
//   SKALA_R2.boot(name, key, cb)   -> load JSON, set window[key], then run cb()
//                                     (the committed *_data.js stays a last-resort
//                                      fallback that already set window[key])
//
// To point at a different bucket / custom domain, change BASE below. Set BASE = ""
// to disable R2 and use the committed copies on GitHub Pages.
(function (g) {
  "use strict";
  var BASE = "https://pub-3d539da10a4c4aa8a3f0048f8dcb067c.r2.dev";
  BASE = BASE.replace(/\/+$/, "");

  function bust() { return "v=" + Date.now(); }

  function url(path) {
    var sep = path.indexOf("?") >= 0 ? "&" : "?";
    return (BASE ? BASE + "/" + path : path) + sep + bust();
  }

  function loadJSON(name) {
    var sources = [];
    if (BASE) sources.push(BASE + "/" + name + "?" + bust());
    sources.push(name + "?" + bust());                 // GitHub Pages copy (same repo)
    var i = 0;
    function tryNext() {
      if (i >= sources.length) return Promise.reject(new Error("no source for " + name));
      var u = sources[i++];
      return fetch(u, { cache: "no-store" }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status + " for " + u);
        return r.json();
      }).catch(tryNext);
    }
    return tryNext();
  }

  function boot(name, key, cb) {
    loadJSON(name)
      .then(function (d) { if (d) g[key] = d; })       // overwrite the *_data.js fallback
      .catch(function () { /* keep whatever the committed *_data.js set */ })
      .then(function () { cb(); });
  }

  g.SKALA_R2 = { base: BASE, url: url, loadJSON: loadJSON, boot: boot };
})(window);
