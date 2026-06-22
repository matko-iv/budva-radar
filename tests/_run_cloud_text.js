// Node runner for the cloud verdict parity test: loads the REAL docs/cloud-text.js
// and runs its interpret + serbianLine + sunDescriptor on a facts JSON, printing
// the result so tests/test_cloud_text_parity.py can compare it to clouds/verdict.py.
// Usage: node _run_cloud_text.js facts.json
const fs = require("fs");
const path = require("path");

global.window = global;                       // the IIFE attaches to `window`
const DOCS = path.join(__dirname, "..", "docs");
(0, eval)(fs.readFileSync(path.join(DOCS, "cloud-text.js"), "utf8"));

const facts = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const params = { frac_clear_max: 0.2, frac_overcast_min: 0.8 };
const res = window.CLOUD_TEXT.interpret(facts, params);
const sr = window.CLOUD_TEXT.serbianLine(facts, res);
const sun = window.CLOUD_TEXT.sunDescriptor(facts);
process.stdout.write(JSON.stringify({
  state: res.state, headline: res.headline, narrative: res.narrative,
  line_sr: sr.text, sun: sun,
}));
