// Shared cell-catalog visualization layer: tracked DHMZ cells (circle sized by
// equivalent diameter, coloured by max dBZ), 30-min velocity arrow, and the
// ±2σ 60-min advection cone the nowcast actually integrates over. Used by BOTH
// index.html (overlay on the dhmz image preview) and radar-map.html (overlay on
// the interactive canvas) — one implementation, identical drawing.
//
// All geometry comes from the data itself (budva_pixel anchor, px_per_km,
// valid_area, radar_site shipped by run.py); the constants below are only
// fallbacks for data.js produced by older pipelines.
(function (global) {
  'use strict';

  var FALLBACK = {
    px_per_km: 1.3648,
    valid_area: [3, 96, 657, 716],
    radar_site: { lat: 42.8944, lon: 17.4783, range_km: 248 },
  };
  var MODERATE_DBZ = 30.0, HEAVY_DBZ = 40.0, SEVERE_DBZ = 50.0;
  var MAX_LABELS = 12;

  function dbzColor(dbz) {
    if (dbz == null || isNaN(dbz)) return '#9e9e9e';
    if (dbz < MODERATE_DBZ) return '#2e7d32';   // light rain — green
    if (dbz < HEAVY_DBZ)    return '#f9a825';   // moderate — yellow
    if (dbz < SEVERE_DBZ)   return '#ef6c00';   // heavy — orange
    return '#c62828';                            // severe — red
  }

  // Pixel <-> lat/lon mapping for one source's image, from its Budva anchor.
  function makeMapping(data, info) {
    var loc = data && data.location;
    var anchor = info && info.budva_pixel;
    if (!loc || !anchor) return null;
    var ppk = (info.px_per_km != null ? info.px_per_km : FALLBACK.px_per_km);
    var kx = 111.32 * Math.cos(loc.lat * Math.PI / 180);
    return {
      pxPerKm: ppk,
      latLonToPx: function (lat, lon) {
        var eKm = (lon - loc.lon) * kx;
        var nKm = (lat - loc.lat) * 110.57;
        return { x: anchor.x + eKm * ppk, y: anchor.y - nKm * ppk };
      },
      pxToLatLon: function (x, y) {
        var eKm = (x - anchor.x) / ppk;
        var nKm = (anchor.y - y) / ppk;
        return { lat: loc.lat + nKm / 110.57, lon: loc.lon + eKm / kx };
      },
    };
  }

  function inValid(info, x, y) {
    var va = (info && info.valid_area) || FALLBACK.valid_area;
    if (!va || va.length !== 4) return true;
    return x >= va[0] && x < va[2] && y >= va[1] && y < va[3];
  }

  function inCoverage(map, info, x, y) {
    var rs = (info && info.radar_site) || FALLBACK.radar_site;
    if (!rs || !map) return true;
    var s = map.latLonToPx(rs.lat, rs.lon);
    var r = rs.range_km * map.pxPerKm;
    var dx = x - s.x, dy = y - s.y;
    return dx * dx + dy * dy <= r * r;
  }

  // The shipped catalog filtered to plausible cells (inside the valid_area AND
  // the radar's coverage disc) — the single source for nowcast replay + drawing.
  function catalogCells(data, info) {
    if (!info || !Array.isArray(info.cells)) return [];
    var map = makeMapping(data, info);
    if (!map) return [];
    return info.cells.filter(function (c) {
      var p = map.latLonToPx(c.lat, c.lon);
      return inValid(info, p.x, p.y) && inCoverage(map, info, p.x, p.y);
    });
  }

  // SVG inner string for the cell layer. domId highlights the dominant cell.
  function buildCellsLayer(data, info, domId) {
    var cells = catalogCells(data, info);
    if (!cells.length) return '';
    var map = makeMapping(data, info);
    var ppk = map.pxPerKm;
    // Label only the strongest dozen (plus the dominant) so a widespread-rain
    // day (100+ cells) doesn't smother the map with text.
    var byDbz = cells.slice().sort(function (a, b) { return (b.max_dbz || 0) - (a.max_dbz || 0); });
    var labelIds = {};
    for (var i = 0; i < Math.min(MAX_LABELS, byDbz.length); i++) labelIds[byDbz[i].id] = true;

    var s = '';
    for (var k = 0; k < cells.length; k++) {
      var c = cells[k];
      var p = map.latLonToPx(c.lat, c.lon);
      var rPx = Math.max(3, (c.equiv_diam_km / 2) * ppk);
      var col = dbzColor(c.max_dbz);
      var isDom = domId != null && c.id === domId;
      var hasVel = c.speed_kmh != null && c.direction_deg != null && c.speed_kmh >= 1;

      if (hasVel) {
        var dir = c.direction_deg;
        // ±2σ cone edge at t=60 min (mirrors nowcast: base + 0.1 deg/min growth)
        var half = 2 * ((c.cell_type === 'convective' ? 15 : 5) + 0.1 * 60);
        var reach = rPx + Math.min(c.speed_kmh, 120) * 1.0 * ppk; // body + 60-min travel
        var pts = [p.x.toFixed(1) + ',' + p.y.toFixed(1)];
        for (var a = dir - half; a <= dir + half + 0.01; a += 6) {
          var t = a * Math.PI / 180;
          pts.push((p.x + reach * Math.sin(t)).toFixed(1) + ',' + (p.y - reach * Math.cos(t)).toFixed(1));
        }
        s += '<polygon points="' + pts.join(' ') + '" fill="' + col + '" fill-opacity="0.08" stroke="' + col + '" stroke-opacity="0.35" stroke-width="0.8"/>';
        // 30-min displacement arrow
        var aLen = Math.min(c.speed_kmh, 120) * 0.5 * ppk;
        var tD = dir * Math.PI / 180;
        var ex = p.x + aLen * Math.sin(tD), ey = p.y - aLen * Math.cos(tD);
        s += '<line x1="' + p.x.toFixed(1) + '" y1="' + p.y.toFixed(1) + '" x2="' + ex.toFixed(1) + '" y2="' + ey.toFixed(1) + '" stroke="' + col + '" stroke-width="2"/>';
        var offs = [150, -150];
        for (var o = 0; o < 2; o++) {
          var tH = (dir + offs[o]) * Math.PI / 180;
          s += '<line x1="' + ex.toFixed(1) + '" y1="' + ey.toFixed(1) + '" x2="' + (ex + 6 * Math.sin(tH)).toFixed(1) + '" y2="' + (ey - 6 * Math.cos(tH)).toFixed(1) + '" stroke="' + col + '" stroke-width="2"/>';
        }
      }

      s += '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="' + rPx.toFixed(1) + '" fill="' + col + '" fill-opacity="0.15" stroke="' + col + '" stroke-width="' + (isDom ? 2.5 : 1.5) + '"/>';
      if (isDom) {
        s += '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="' + (rPx + 5).toFixed(1) + '" fill="none" stroke="' + col + '" stroke-width="1.5" stroke-dasharray="5 3"/>';
      }
      if (isDom || labelIds[c.id]) {
        s += '<text x="' + p.x.toFixed(1) + '" y="' + (p.y - rPx - 4).toFixed(1) + '" fill="' + col + '" stroke="#fff" stroke-width="2.5" paint-order="stroke" font-size="11" font-weight="600" text-anchor="middle">'
          + Math.round(c.max_dbz) + ' dBZ' + (hasVel ? ' · ' + Math.round(c.speed_kmh) + ' km/h' : '') + '</text>';
      }
    }
    return s ? '<g class="cells-layer">' + s + '</g>' : '';
  }

  global.SKALA_CELLS = {
    makeMapping: makeMapping,
    inValid: inValid,
    inCoverage: inCoverage,
    catalogCells: catalogCells,
    buildCellsLayer: buildCellsLayer,
    dbzColor: dbzColor,
  };
})(typeof window !== 'undefined' ? window : globalThis);
