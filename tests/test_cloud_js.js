// Verify the browser JS (cloud-text.js + cloud-nowcast-browser.js +
// cloud-sections.js) against the generated docs/cloud_data.js:
//  (1) the JS state machine reproduces the Python cloud_verdict state for Budva,
//  (2) the per-point nowcast runs and behaves sanely (a clear point with a deck
//      advecting in reads as approaching).
// Run:  node tests/test_cloud_js.js   (exit 0 = pass)
const fs = require("fs");
const path = require("path");

global.window = global;                       // the IIFEs attach to `window`
const DOCS = path.join(__dirname, "..", "docs");
function load(f) { (0, eval)(fs.readFileSync(path.join(DOCS, f), "utf8")); }

load("cloud-text.js");
load("cloud-nowcast-browser.js");
load("cloud-sections.js");
load("cloud_data.js");                          // sets window.CLOUD_DATA

const D = window.CLOUD_DATA;
const fails = [];
function check(name, cond, extra) {
  if (!cond) fails.push(name + (extra ? ": " + extra : ""));
  console.log((cond ? "PASS  " : "FAIL  ") + name + (cond ? "" : " :: " + (extra || "")));
}

// (1) JS state machine == Python verdict state for the same facts
const py = (D.summary.cloud_verdict || {}).state;
const jsRes = window.CLOUD_TEXT.interpret(D.facts, D.params);
check("js interpret matches python state (" + py + ")", jsRes.state === py, "js=" + jsRes.state);

// cloudHeadline prefers the precomputed verdict
const h = window.CLOUD_SECTIONS.cloudHeadline(D);
check("cloudHeadline returns python headline", h.headline === D.summary.cloud_verdict.headline, h.headline);

// (2) per-point nowcast runs on the coarse grid
const loc = D.location;
const f = window.CLOUD_NOWCAST.pointFacts(D.field, loc.lat, loc.lon, D.params, "Budva");
check("pointFacts produces a fraction", f.cloudFracNow !== undefined && f.cloudFracNow !== null, JSON.stringify(f.cloudFracNow));
check("pointFacts has rings", Array.isArray(f.rings) && f.rings.length > 0);
const pr = window.CLOUD_TEXT.interpret(f, D.params);
check("pointFacts -> a valid state", !!window.CLOUD_TEXT.STATE_META[pr.state], pr.state);
// In the demo a deck advects toward Budva from the west -> approaching/partly,
// definitely not plain CLEAR. (Skipped gracefully for non-demo data.)
if ((D.source || {}).data_source === "demo") {
  check("demo: Budva not plain CLEAR (deck incoming)", pr.state !== "CLEAR", pr.state);
}

if (fails.length) { console.log("\n" + fails.length + " failure(s)."); process.exit(1); }
console.log("\nPASS — cloud JS port OK.");
