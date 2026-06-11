// Test helper: run the browser nowcast port on a JSON {cells, lat, lon} payload
// (path in argv[2]) and print the result JSON to stdout. Used by
// test_nowcast_parity.py to compare against the Python nowcast.
const fs = require('fs');
const path = require('path');
const NOWCAST = require(path.join(__dirname, '..', 'docs', 'nowcast-browser.js'));
const inp = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.stdout.write(JSON.stringify(NOWCAST.arrivalNowcast(inp.cells, inp.lat, inp.lon)));
