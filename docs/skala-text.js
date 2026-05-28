// Shared interpretation wording for index.html and radar-map.html so BOTH
// pages produce literally identical text for the same location.
//
// Pixel-first model (per spec):
//   RAINING      - the exact pixel under the marker is wet (it is raining here)
//   APPROACHING  - rain is heading toward us and will reach the location
//   BYPASSING    - rain is nearby but not heading here (will miss us)
//   NO_RAIN      - nothing falling (clear / only noise / scattered sub-threshold)
// Intensity (light / moderate / heavy / hail) comes from the closest cell's dBZ.

(function (global) {
  'use strict';

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

  // state -> banner styling + base headline (location is appended by interpret()).
  var STATE_META = {
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

    if (facts.rainAtLocation) {
      state = 'RAINING';
      narrative = 'Raining now — ' + intensity + dbzTxt + '.';
    } else if (facts.approaching) {
      state = 'APPROACHING';
      var eta = (facts.eta != null && !isNaN(facts.eta)) ? ', ETA ~' + Math.round(facts.eta) + ' min' : '';
      narrative = 'Rain approaching — ' + intensity + ', ' + where + eta + '.';
    } else if (facts.anyRain) {
      state = 'BYPASSING';
      var moving = facts.motionCardinal ? ' (moving ' + facts.motionCardinal + ')' : '';
      narrative = 'Rain nearby but not heading here — ' + intensity + ', ' + where + moving + '.';
    } else {
      state = 'NO_RAIN';
      if (facts.anyWet) narrative = 'Scattered radar echoes below the rain threshold — not falling.';
      else if (facts.anyEcho) narrative = 'Only weak echo on the radar — likely noise, not rain.';
      else narrative = 'No rain on the radar within 150 km.';
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
