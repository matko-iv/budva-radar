// Shared interpretation wording for index.html and radar-map.html so BOTH
// pages produce literally identical text for the same location.
//
// Pixel-first model (per spec):
//   RAINING - the location is wet right NOW (it is raining here)
//   NO_RAIN - not raining here now (clear / noise / rain only elsewhere)
// BINARY current-state verdict: the forecast (approaching / ETA) moved to SKALA
// NOWCAST. STATE_META still lists the old states for back-compat styling only.
// Intensity (light / moderate / heavy / hail) comes from the closest cell's dBZ.

(function (global) {
  'use strict';

  // Only rain within this radius (km) of the point is called "nearby". Rain
  // farther out is not a concern for the location — reported as no rain
  // heading here, with the nearest-echo distance noted (not "no rain at all").
  var SKALA_VICINITY_KM = 20;

  // dBZ at/above which an APPROACHING cell is a severe threat (hail / Mie
  // scattering) — mirrors config.SEVERE_DBZ. Judged on the dominant tracked
  // storm cell, which may be a big cell BEHIND a lighter closest one, so a
  // severe storm is never masked by light rain in front of it.
  var SEVERE_DBZ = 50;

  function skalaIntensity(dbz) {
    if (dbz == null || isNaN(dbz)) return 'rain';
    if (dbz < 25) return 'light rain';
    if (dbz < 40) return 'moderate rain';
    if (dbz < 50) return 'heavy rain';
    return 'hail';
  }

  function fmtKm(km) {
    if (km == null) return '?';
    if (km < 1) return km.toFixed(2);
    if (km < 10) return km.toFixed(1);
    return Math.round(km).toString();
  }

  // Honest expectations (PDF Part C4/E): flag an ETA beyond the deterministic
  // skill horizon as probabilistic. Mirrors radar/verdict.py DETERMINISTIC_ETA.
  var DETERMINISTIC_ETA_MAX_MIN = 30;
  function etaText(eta) {
    if (eta == null || isNaN(eta)) return '';
    var r = Math.round(eta);
    return ', ETA ~' + r + ' min' + (r > DETERMINISTIC_ETA_MAX_MIN ? ' (probabilistic)' : '');
  }

  // state -> banner styling + base headline (location is appended by interpret()).
  var STATE_META = {
    SEVERE:      { cls: 'severe', bg: '#6a1b9a', fg: '#fff',   head: 'SEVERE STORM' },
    RAINING:     { cls: 'warn', bg: '#1565c0', fg: '#fff',     head: 'RAINING NOW' },
    APPROACHING: { cls: 'warn', bg: '#c62828', fg: '#fff',     head: 'RAIN APPROACHING' },
    BYPASSING:   { cls: 'ok',   bg: '#9e9e9e', fg: '#fff',     head: 'RAIN NEARBY — BYPASSING' },
    NO_RAIN:     { cls: 'ok',   bg: '#a5d6a7', fg: '#1b3a1c',  head: 'NO RAIN' },
  };

  // facts: {
  //   locationName, rainAtLocation, approaching, anyRain, anyWet, anyEcho,
  //   km, cardinal, dbz, motionCardinal, eta
  // }
  function skalaInterpret(facts) {
    facts = facts || {};
    var loc = facts.locationName || 'this location';
    var intensity = skalaIntensity(facts.dbz);
    var dbzTxt = (facts.dbz != null && !isNaN(facts.dbz)) ? ' (' + Math.round(facts.dbz) + ' dBZ)' : '';
    var where = '~' + fmtKm(facts.km) + ' km' + (facts.cardinal ? ' ' + facts.cardinal : '');
    var state, narrative;

    // BINARY: is it RAINING here right NOW, or not. The forecast (approaching /
    // ETA / severe-incoming) is SKALA NOWCAST's job now — this is current-state
    // only. Must stay identical to radar/verdict.py interpret(); parity test enforces it.
    if (facts.rainAtLocation) {
      state = 'RAINING';
      narrative = 'Raining now — ' + intensity + dbzTxt + '.';
    } else {
      state = 'NO_RAIN';
      if (facts.anyRain && facts.km != null && !isNaN(facts.km)) {
        narrative = 'No rain at ' + loc + ' (nearest echo ' + where + ').';
      } else if (facts.anyWet) {
        narrative = 'Scattered radar echoes below the rain threshold — not falling.';
      } else if (facts.anyEcho) {
        narrative = 'Only weak echo on the radar — likely noise, not rain.';
      } else {
        narrative = 'No rain on the radar within 150 km.';
      }
    }

    var meta = STATE_META[state];
    return {
      state: state,
      headline: meta.head + ' — ' + loc,
      narrative: narrative,
      meta: meta,
    };
  }

  global.SKALA = {
    intensity: skalaIntensity,
    interpret: skalaInterpret,
    STATE_META: STATE_META,
    fmtKm: fmtKm,
  };
})(window);
