// Smoke: syntax-check every page's inline scripts + exercise the shared
// modules (skala-cells-viz + skala-sections + nowcast-browser) against the
// live data.js the way index.html and radar-map.html do.
const fs = require("fs");

function inlineScripts(html) {
  const out = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html)) !== null) if (m[1].trim()) out.push(m[1]);
  return out;
}

// 1) syntax-check all inline scripts on every surface
const pages = [
  "docs/index.html",
  "docs/radar-map.html",
  "../matko/docs/forecast.html",
  "C:/Users/Matija/weather-forecast/docs/forecast.html",
];
for (const p of pages) {
  const html = fs.readFileSync(p, "utf8");
  const scripts = inlineScripts(html);
  scripts.forEach((s, i) => { new Function(s); });
  console.log(`syntax OK: ${p} (${scripts.length} inline script(s))`);
}

// 2) shared-module behaviour against live data.js
const w = {};
global.window = w;
eval(fs.readFileSync("docs/data.js", "utf8"));
eval(fs.readFileSync("docs/skala-text.js", "utf8"));
eval(fs.readFileSync("docs/skala-cells-viz.js", "utf8"));
eval(fs.readFileSync("docs/skala-sections.js", "utf8"));
const d = w.RADAR_DATA;
const dh = d.sources.dhmz;

// headline comes from the precomputed verdict
const head = w.SKALA_SECTIONS.budvaHeadline(d);
if (!d.summary.budva_verdict) throw new Error("no precomputed verdict in data.js");
if (head.state !== d.summary.budva_verdict.state) throw new Error("headline != precomputed verdict");
if (head.narrative !== d.summary.budva_verdict.narrative) throw new Error("narrative mismatch");
console.log("budvaHeadline uses precomputed verdict:", head.state);

// cell layer renders from the catalog on the dhmz geometry
const layer = w.SKALA_CELLS.buildCellsLayer(d, dh, null);
const nCircles = (layer.match(/<circle/g) || []).length;
const nCones = (layer.match(/<polygon/g) || []).length;
const kept = w.SKALA_CELLS.catalogCells(d, dh).length;
console.log(`cells layer: ${kept} cells kept of ${dh.cells.length}, circles=${nCircles}, cones=${nCones}`);
if (!nCircles) throw new Error("no circles rendered");

// dominant highlight + mapping round-trip
const map = w.SKALA_CELLS.makeMapping(d, dh);
const ll = map.pxToLatLon(300, 400);
const p = map.latLonToPx(ll.lat, ll.lon);
if (Math.abs(p.x - 300) > 0.01 || Math.abs(p.y - 400) > 0.01) throw new Error("mapping round-trip");

// nowcast replay for a clicked point still works on the filtered catalog
const NOWCAST = require("./docs/nowcast-browser.js");
const cells = w.SKALA_CELLS.catalogCells(d, dh);
const nc = NOWCAST.arrivalNowcast(cells, d.location.lat, d.location.lon);
console.log("nowcast replay at Budva: approaching=" + nc.approaching +
  " p60=" + nc.p_by_lead["60"] + " (pipeline says " + dh.approaching.is_approaching + ")");
if (nc.approaching !== dh.approaching.is_approaching) {
  throw new Error("JS replay disagrees with pipeline at Budva");
}
console.log("PAGES SMOKE OK");
