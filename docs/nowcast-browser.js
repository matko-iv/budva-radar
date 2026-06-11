// Browser port of the probabilistic arrival nowcast (nowcast.py) so radar-map.html
// can run the SAME per-cell, multi-lead model for ANY clicked point — fed by the
// cell catalog shipped in data.js (sources.dhmz.cells). Budva itself stays pinned
// to the precomputed pipeline result; this is for exploratory clicks elsewhere.
//
// FAITHFUL MIRROR of nowcast.py (_lifetime_min, _cell_arrival, arrival_nowcast) +
// colormap.classify_intensity + calibration.bearing_to_cardinal. A parity test
// (tests/test_nowcast_parity.py) asserts this matches the Python output for Budva.
//
// IMPORTANT: the constants in C below MUST match config.py. If config.py changes,
// change them here too (the parity test will catch drift).
//
// UMD: attaches window.NOWCAST in the browser, exports in Node (for the test).
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.NOWCAST = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var C = {
    RAIN_DBZ_THRESHOLD: 20.0,
    NOISE_DBZ: 5.0,
    MODERATE_DBZ: 30.0,
    HEAVY_DBZ_THRESHOLD: 40.0,
    SEVERE_DBZ: 50.0,
    EXTREME_DBZ: 55.0,
    NOWCAST_MIN_LIFETIME_MIN: 15.0,
    NOWCAST_REACH_BUFFER_KM: 5.0,
    NOWCAST_MAX_SPEED_KMH: 120.0,
    NOWCAST_SPEED_FACTORS: [0.8, 0.9, 1.0, 1.1, 1.2],
    NOWCAST_LEAD_STEPS_MIN: 5,
    NOWCAST_LEAD_MAX_MIN: 120,
    NOWCAST_DIR_SPREAD_CONVECTIVE_DEG: 15.0,
    NOWCAST_DIR_SPREAD_STRATIFORM_DEG: 5.0,
    NOWCAST_DIR_GROWTH_DEG_PER_MIN: 0.1,
    P_APPROACH_THRESHOLD: 0.25,
    APPROACH_LEAD_MIN: 60,
    APPROACH_MAX_DIST_KM: 50.0,
  };
  var LEAD_BUCKETS = [15, 30, 60, 120];

  var rad = function (d) { return d * Math.PI / 180.0; };
  var round3 = function (x) { return Math.round(x * 1000) / 1000; };
  var round1 = function (x) { return Math.round(x * 10) / 10; };

  // colormap.classify_intensity
  function classifyIntensity(dbz) {
    if (dbz === null || dbz === undefined || (typeof dbz === 'number' && isNaN(dbz))) return 'no precipitation';
    if (dbz < C.NOISE_DBZ) return 'no precipitation';
    if (dbz < 15.0) return 'noise / clear-air';
    if (dbz < C.RAIN_DBZ_THRESHOLD) return 'trace (sub-rain)';
    if (dbz < 25.0) return 'light rain';
    if (dbz < C.MODERATE_DBZ) return 'light to moderate rain';
    if (dbz < C.HEAVY_DBZ_THRESHOLD) return 'moderate rain';
    if (dbz < 45.0) return 'heavy rain';
    if (dbz < C.SEVERE_DBZ) return 'very heavy rain';
    if (dbz < C.EXTREME_DBZ) return 'severe (likely hail)';
    return 'extreme (hail core)';
  }

  // calibration.bearing_to_cardinal (8-point, +22.5 deg sectors)
  function bearingToCardinal(deg) {
    deg = ((deg % 360) + 360) % 360;
    var dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
    return dirs[Math.floor((deg + 22.5) / 45) % 8];
  }

  // nowcast._lifetime_min — survival timescale (min); null = survives the window.
  function lifetimeMin(cell) {
    var slope = cell.dbz_trend_per_min;
    if (slope === null || slope === undefined || slope >= -1e-3) return null;
    var head = Math.max(cell.max_dbz - C.RAIN_DBZ_THRESHOLD, 0.0);
    return Math.max(head / Math.abs(slope), C.NOWCAST_MIN_LIFETIME_MIN);
  }

  // Pre-normalized unscented weights (speed x direction), matching nowcast.py.
  var SF = C.NOWCAST_SPEED_FACTORS;
  var SW = (function () {
    var w = SF.map(function (f) { return Math.exp(-0.5 * Math.pow((f - 1.0) / 0.2, 2)); });
    var s = w.reduce(function (a, b) { return a + b; }, 0);
    return w.map(function (x) { return x / s; });
  })();
  var DOFF = [-2.0, -1.0, 0.0, 1.0, 2.0];
  var DW = (function () {
    var w = DOFF.map(function (o) { return Math.exp(-0.5 * Math.pow(o / 1.0, 2)); });
    var s = w.reduce(function (a, b) { return a + b; }, 0);
    return w.map(function (x) { return x / s; });
  })();

  // nowcast._cell_arrival — per-cell arrival prob + ETA + per-lead-bucket prob,
  // relative to point (latP, lonP). Returns null if the cell has no usable
  // velocity. Geometry (edge_km, bearing_deg) is attached to every return so the
  // caller can describe the dominant cell relative to the point.
  function cellArrival(cell, latP, lonP) {
    var kx = 111.32 * Math.cos(rad(latP));
    var ky = 110.57;
    var px = (cell.lon - lonP) * kx;   // east position of centre (km)
    var py = (cell.lat - latP) * ky;   // north position of centre (km)
    var distCentroid = Math.hypot(px, py);
    var edgeKm = Math.max(0.0, distCentroid - cell.equiv_diam_km / 2.0);
    var bearingDeg = (Math.atan2(px, py) * 180.0 / Math.PI + 360) % 360;
    var geo = { edge_km: edgeKm, bearing_deg: bearingDeg };
    var zero = function (extra) {
      var pbl = {}; for (var k = 0; k < LEAD_BUCKETS.length; k++) pbl[LEAD_BUCKETS[k]] = extra.pbl;
      return Object.assign({ p: extra.p, eta_min: extra.eta, p_by_lead: pbl,
        tau_min: null, stationary: !!extra.stationary, on_location: !!extra.on_location }, geo);
    };

    // Cell body covers the point -> raining there NOW.
    if (edgeKm <= 0) {
      return zero({ p: 1.0, eta: 0.0, pbl: 1.0, on_location: true });
    }

    // Receding test (mirrors nowcast.py): positive range rate = centre moving
    // AWAY. A departing cell whose trailing edge still sits within the buffer
    // has already PASSED — it must not read "approaching, ETA 0".
    var receding = false;
    if (cell.speed_kmh != null && cell.direction_deg != null) {
      var spd0 = Math.min(parseFloat(cell.speed_kmh) || 0.0, C.NOWCAST_MAX_SPEED_KMH);
      if (spd0 >= 1.0) {
        var ang0 = rad(cell.direction_deg);
        receding = (px * Math.sin(ang0) + py * Math.cos(ang0)) > 0;
      }
    }

    // Nearest edge essentially on top of us: imminent if inbound/stationary/
    // unknown motion; already-passed if receding.
    if (edgeKm <= C.NOWCAST_REACH_BUFFER_KM) {
      if (receding) return zero({ p: 0.0, eta: null, pbl: 0.0 });
      return zero({ p: 1.0, eta: 0.0, pbl: 1.0, on_location: true });
    }

    if (cell.speed_kmh === null || cell.speed_kmh === undefined || cell.direction_deg === null || cell.direction_deg === undefined) {
      return null;
    }
    var speed = Math.min(parseFloat(cell.speed_kmh) || 0.0, C.NOWCAST_MAX_SPEED_KMH);
    var direction = cell.direction_deg;
    if (speed < 1.0) return zero({ p: 0.0, eta: null, pbl: 0.0, stationary: true });

    var maxReachKm = C.NOWCAST_MAX_SPEED_KMH * (C.NOWCAST_LEAD_MAX_MIN / 60.0);
    if (edgeKm > maxReachKm) return zero({ p: 0.0, eta: null, pbl: 0.0 });

    var reach = cell.equiv_diam_km / 2.0 + C.NOWCAST_REACH_BUFFER_KM;
    var convective = cell.cell_type === 'convective';
    var baseSpread = convective ? C.NOWCAST_DIR_SPREAD_CONVECTIVE_DEG : C.NOWCAST_DIR_SPREAD_STRATIFORM_DEG;
    var tau = lifetimeMin(cell);

    var dt = C.NOWCAST_LEAD_STEPS_MIN;
    var Tmax = C.NOWCAST_LEAD_MAX_MIN;
    var buckets = {}; for (var b = 0; b < LEAD_BUCKETS.length; b++) buckets[LEAD_BUCKETS[b]] = 0.0;
    var hitW = 0.0, etaAcc = 0.0, etaWsum = 0.0;

    for (var i = 0; i < SF.length; i++) {
      var v = speed * SF[i] / 60.0;                 // km/min
      for (var j = 0; j < DOFF.length; j++) {
        var w = SW[i] * DW[j];
        var t = dt, hitT = null;
        while (t <= Tmax) {
          var spread = baseSpread + C.NOWCAST_DIR_GROWTH_DEG_PER_MIN * t;
          var ang = rad(direction + DOFF[j] * spread);
          var ex = px + v * t * Math.sin(ang);
          var ny = py + v * t * Math.cos(ang);
          if (Math.hypot(ex, ny) <= reach) { hitT = t; break; }
          t += dt;
        }
        if (hitT === null) continue;
        var surv = tau === null ? 1.0 : Math.exp(-hitT / tau);
        if (cell.trend === 'growing') surv = Math.max(surv, 0.8);
        hitW += w * surv;
        etaAcc += w * surv * hitT;
        etaWsum += w * surv;
        for (var bb = 0; bb < LEAD_BUCKETS.length; bb++) {
          if (hitT <= LEAD_BUCKETS[bb]) buckets[LEAD_BUCKETS[bb]] += w * surv;
        }
      }
    }

    var p = Math.min(Math.max(hitW, 0.0), 1.0);
    var eta = etaWsum > 1e-6 ? etaAcc / etaWsum : null;
    var pbl = {};
    for (var k2 = 0; k2 < LEAD_BUCKETS.length; k2++) {
      pbl[LEAD_BUCKETS[k2]] = round3(Math.min(buckets[LEAD_BUCKETS[k2]], 1.0));
    }
    return Object.assign({
      p: round3(p),
      eta_min: eta === null ? null : round1(eta),
      p_by_lead: pbl,
      tau_min: tau === null ? null : round1(tau),
      stationary: false,
      on_location: false,
    }, geo);
  }

  // nowcast.arrival_nowcast — combine per-cell arrival into one P(rain) + ETA +
  // confidence curve + dominant cell, for point (latP, lonP).
  function arrivalNowcast(cells, latP, lonP) {
    cells = cells || [];
    var per = [];
    for (var i = 0; i < cells.length; i++) {
      var a = cellArrival(cells[i], latP, lonP);
      if (a !== null) per.push([cells[i], a]);
    }
    if (!per.length) {
      var z = {}; for (var k = 0; k < LEAD_BUCKETS.length; k++) z[String(LEAD_BUCKETS[k])] = 0.0;
      return { p_rain: 0.0, eta_minutes: null, n_cells_considered: 0,
        approaching: false, dominant: null, p_by_lead: z };
    }

    var agg = {};
    for (var bi = 0; bi < LEAD_BUCKETS.length; bi++) {
      var b = LEAD_BUCKETS[bi];
      var prod = 1.0;
      for (var p2 = 0; p2 < per.length; p2++) {
        var a2 = per[p2][1];
        var pb = (a2.p_by_lead[b] !== undefined) ? a2.p_by_lead[b]
          : (b === LEAD_BUCKETS[LEAD_BUCKETS.length - 1] ? a2.p : 0.0);
        prod *= (1.0 - pb);
      }
      agg[String(b)] = round3(1.0 - prod);
    }
    var pRain = agg[String(LEAD_BUCKETS[LEAD_BUCKETS.length - 1])];

    // dominant cell = highest p, tie-break earliest ETA
    per.sort(function (a, b2) {
      var pa = -a[1].p, pb = -b2[1].p;
      if (pa !== pb) return pa - pb;
      var ea = a[1].eta_min === null ? 1e9 : a[1].eta_min;
      var eb = b2[1].eta_min === null ? 1e9 : b2[1].eta_min;
      return ea - eb;
    });
    var domCell = per[0][0], domA = per[0][1];
    var dominant = {
      track_id: domCell.id,
      p: domA.p,
      eta_minutes: domA.eta_min,
      dist_km: domA.edge_km,
      bearing_deg: domA.bearing_deg,
      bearing_cardinal: bearingToCardinal(domA.bearing_deg),
      max_dbz: domCell.max_dbz,
      cell_type: domCell.cell_type,
      trend: domCell.trend === undefined ? null : domCell.trend,
      speed_kmh: domCell.speed_kmh === undefined ? null : domCell.speed_kmh,
      direction_cardinal: (domCell.direction_deg === null || domCell.direction_deg === undefined)
        ? null : bearingToCardinal(domCell.direction_deg),
      intensity_label: classifyIntensity(domCell.max_dbz),
      on_location: !!domA.on_location,
    };
    // Approaching verdict: 60-min bucket + dominant-distance gate (mirrors
    // nowcast.py; domA.edge_km is already relative to the assessed point).
    var pLead = agg[String(C.APPROACH_LEAD_MIN)];
    if (pLead === undefined) pLead = pRain;
    return {
      p_rain: pRain,
      eta_minutes: domA.eta_min,
      n_cells_considered: per.length,
      approaching: pLead >= C.P_APPROACH_THRESHOLD && domA.edge_km <= C.APPROACH_MAX_DIST_KM,
      dominant: dominant,
      p_by_lead: agg,
    };
  }

  return {
    arrivalNowcast: arrivalNowcast,
    cellArrival: cellArrival,
    lifetimeMin: lifetimeMin,
    classifyIntensity: classifyIntensity,
    bearingToCardinal: bearingToCardinal,
    _constants: C,
  };
}));
