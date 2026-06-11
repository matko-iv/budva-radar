// Node runner for the verdict parity test: loads the REAL skala-text.js +
// skala-sections.js, feeds them a status JSON (with any precomputed verdict
// stripped by the caller so the JS computation path runs), and prints the
// budvaHeadline result. Usage: node _run_skala.js payload.json
const fs = require("fs");
const path = require("path");

const w = {};
global.window = w;
const docs = path.join(__dirname, "..", "docs");
eval(fs.readFileSync(path.join(docs, "skala-text.js"), "utf8"));
eval(fs.readFileSync(path.join(docs, "skala-sections.js"), "utf8"));

const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const res = w.SKALA_SECTIONS.budvaHeadline(data);
process.stdout.write(JSON.stringify({
  state: res.state, headline: res.headline, narrative: res.narrative,
}));
