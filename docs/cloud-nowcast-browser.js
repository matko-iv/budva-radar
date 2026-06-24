// cloud-nowcast-browser.js — browser port of clouds/nowcast.py + the coarse
// parts of clouds/interpret.py, so clicking any point on cloud-map.html reruns
// the field-advection nowcast for that point on the coarse grid shipped in
// CLOUD_DATA.field. Keep in sync with the Python.
(function (global) {
  var KM_PER_DEG = 111.32, MIN_CONF = 0.12, MIN_SPEED = 2.0, SAMPLE_R = 8.0;
  var OPP = { N: "S", S: "N", E: "W", W: "E", NE: "SW", SW: "NE", SE: "NW", NW: "SE" };
  var COMMON = {
    "high|thin": "cirrus", "high|thick": "cirrostratus",
    "mid|thin": "altocumulus", "mid|thick": "altostratus",
    "low|thin": "stratocumulus", "low|thick": "stratus"
  };

  function toRad(d) { return d * Math.PI / 180; }
  function haversineKm(la1, lo1, la2, lo2) {
    var r1 = toRad(la1), r2 = toRad(la2), dla = toRad(la2 - la1), dlo = toRad(lo2 - lo1);
    var a = Math.sin(dla / 2) ** 2 + Math.cos(r1) * Math.cos(r2) * Math.sin(dlo / 2) ** 2;
    return 2 * 6371 * Math.asin(Math.sqrt(a));
  }
  function dest(lat, lon, bearing, distKm) {
    var b = toRad(bearing);
    return [lat + (distKm * Math.cos(b)) / KM_PER_DEG,
            lon + (distKm * Math.sin(b)) / (KM_PER_DEG * Math.cos(toRad(lat)))];
  }
  function contains(field, lat, lon) {
    var la = field.lats, lo = field.lons;
    var laMin = Math.min(la[0], la[la.length - 1]), laMax = Math.max(la[0], la[la.length - 1]);
    var loMin = Math.min(lo[0], lo[lo.length - 1]), loMax = Math.max(lo[0], lo[lo.length - 1]);
    return lat >= laMin && lat <= laMax && lon >= loMin && lon <= loMax;
  }

  // Mean of `layer` over grid cells within radiusKm. If cloudyOnly, restrict to
  // cells where frac >= 0.5. Returns null if no valid cell.
  function discMean(field, layer, lat, lon, radiusKm, cloudyOnly) {
    var la = field.lats, lo = field.lons, A = field[layer], F = field.frac;
    if (!A) return null;
    var dDeg = radiusKm / KM_PER_DEG + Math.abs(la[1] - la[0]);
    var sum = 0, n = 0;
    for (var i = 0; i < la.length; i++) {
      if (Math.abs(la[i] - lat) > dDeg + 1) continue;
      for (var j = 0; j < lo.length; j++) {
        var v = A[i][j];
        if (v == null) continue;
        if (cloudyOnly && !(F[i][j] != null && F[i][j] >= 0.5)) continue;
        if (haversineKm(lat, lon, la[i], lo[j]) > radiusKm) continue;
        sum += v; n++;
      }
    }
    return n ? sum / n : null;
  }
  function cloudFraction(field, lat, lon, radiusKm) {
    return discMean(field, "frac", lat, lon, radiusKm, false);
  }
  function opaqueFraction(field, lat, lon, radiusKm) {
    return discMean(field, "opaque", lat, lon, radiusKm, false);
  }

  function fanFraction(field, lat, lon, motion, t, p) {
    var distKm = motion.speed_kmh * (t / 60);
    var upB = (motion.direction_deg + 180) % 360;
    var spread = p.nowcast_dir_spread_deg + p.nowcast_dir_growth_deg_per_min * t;
    var members = [[-spread, 0.25], [0, 0.5], [spread, 0.25]];
    var num = 0, den = 0;
    for (var k = 0; k < members.length; k++) {
      var d = dest(lat, lon, upB + members[k][0], distKm);
      if (!contains(field, d[0], d[1])) continue;
      var fr = cloudFraction(field, d[0], d[1], SAMPLE_R);
      if (fr == null) continue;
      num += members[k][1] * fr; den += members[k][1];
    }
    return den ? num / den : null;
  }

  function pointNowcast(field, motion, lat, lon, params) {
    var p = params || {};
    // Tight read so clicking a SMALL cloud registers it (a 10 km disc averages it
    // into the surrounding clear sky and reads "clear"). Keep in sync with Python.
    var nowR = p.point_read_radius_km || p.sample_radius_now_km || 10;
    var fracNow = cloudFraction(field, lat, lon, nowR);
    var out = {
      cloudFracNow: fracNow == null ? null : +fracNow.toFixed(3),
      cloudAtLocation: fracNow != null && fracNow > p.frac_clear_max,
      approaching: false, clearing: false, etaMin: null,
      motionCardinal: motion ? motion.direction_cardinal : null,
      series: []
    };
    if (fracNow == null) return out;

    var usable = motion && motion.direction_deg != null
      && (motion.confidence || 0) >= MIN_CONF && (motion.speed_kmh || 0) >= MIN_SPEED;
    var series = [{ t: 0, frac: +fracNow.toFixed(3) }];
    if (!usable) { out.series = series; return out; }

    var step = p.nowcast_lead_step_min, leadMax = p.nowcast_lead_max_min;
    var nowClear = fracNow <= p.frac_clear_max, etaA = null, etaC = null;
    for (var t = step; t <= leadMax; t += step) {
      var fr = fanFraction(field, lat, lon, motion, t, p);
      if (fr == null) break;
      series.push({ t: t, frac: +fr.toFixed(3) });
      if (nowClear && etaA == null && fr > p.frac_clear_max) etaA = t;
      if (!nowClear && etaC == null && fr <= p.frac_clear_max) etaC = t;
    }
    out.series = series;
    if (nowClear) { out.approaching = etaA != null; out.etaMin = etaA; }
    else { out.clearing = etaC != null; out.etaMin = etaC; }
    return out;
  }

  function heightBand(cth, p) {
    if (cth == null) return null;
    if (cth < p.height_low_max_m) return "low";
    if (cth < p.height_mid_max_m) return "mid";
    return "high";
  }
  function thickness(cot, p) { return cot == null ? null : (cot <= p.cot_thin_max ? "thin" : "thick"); }
  function typeLabel(band, thick) {
    if (!band) return null;
    var base = thick ? band + " " + thick + " cloud" : band + " cloud";
    var name = COMMON[band + "|" + thick];
    return name ? base + " (" + name + ")" : base;
  }
  function sunOutlook(nowClear, appr, clr, overcast, eta) {
    if (nowClear) return (appr && eta != null) ? "Sunny now; clouds in ~" + Math.round(eta) + " min."
      : "Sunny — sky stays clear for the next ~2 h.";
    if (clr && eta != null) return "Sun in ~" + Math.round(eta) + " min.";
    if (overcast) return "Sky stays closed for the next ~2 h.";
    return "Variable — some sun likely.";
  }

  // Full facts for one clicked point (feeds CLOUD_TEXT.interpret).
  function pointFacts(field, lat, lon, params, locName) {
    var p = params || {};
    var motion = field.motion;
    var nc = pointNowcast(field, motion, lat, lon, p);
    var frac = nc.cloudFracNow;
    var radii = p.sample_radii_km || [10, 25, 50, 100, 150];
    var descR = radii.length > 1 ? radii[1] : 25;
    var cloudy = frac != null && frac > p.frac_clear_max;
    var cth = cloudy ? discMean(field, "cth", lat, lon, descR, true) : null;
    var cot = cloudy ? discMean(field, "cot", lat, lon, descR, true) : null;
    var band = heightBand(cth, p), thick = thickness(cot, p);

    // Effective sky cover: opaque cloud blocks fully, semitransparent counts
    // little (mirror clouds/interpret.py — uses the CLM-derived opaque layer).
    var opq = opaqueFraction(field, lat, lon, p.point_read_radius_km || p.sample_radius_now_km || 10);
    var w = p.semi_sky_weight != null ? p.semi_sky_weight : 0.25;
    var sky = frac == null ? null : ((opq || 0) + w * Math.max(frac - (opq || 0), 0));
    var thinVeil = frac != null && frac > p.frac_clear_max
      && sky != null && sky <= p.frac_clear_max;

    var nowClear = sky != null && sky <= p.frac_clear_max;
    var overcast = sky != null && sky >= p.frac_overcast_min;
    var rings = radii.map(function (r) {
      var rf = cloudFraction(field, lat, lon, r);
      var ro = opaqueFraction(field, lat, lon, r);
      return { radius_km: r, cloud_fraction: rf == null ? null : +rf.toFixed(3),
               opaque_fraction: ro == null ? null : +ro.toFixed(3) };
    });
    return {
      locationName: locName || "this point",
      cloudFracNow: frac,
      opaqueFracNow: opq == null ? null : +opq.toFixed(3),
      skyCoverEff: sky == null ? null : +sky.toFixed(3),
      thinVeil: thinVeil,
      cloudAtLocation: nc.cloudAtLocation,
      approaching: nc.approaching, clearing: nc.clearing, etaMin: nc.etaMin,
      motionCardinal: nc.motionCardinal, fromCardinal: OPP[nc.motionCardinal],
      cloudTopHeightM: cth == null ? null : Math.round(cth),
      cloudTopTempC: null, heightBand: band,
      opticalThickness: cot == null ? null : +cot.toFixed(1), thickness: thick,
      phase: null, cloudTypeLabel: typeLabel(band, thick),
      sunOutlook: sunOutlook(nowClear, nc.approaching, nc.clearing, overcast, nc.etaMin),
      rings: rings, series: nc.series
    };
  }

  global.CLOUD_NOWCAST = {
    pointNowcast: pointNowcast, pointFacts: pointFacts,
    cloudFraction: cloudFraction, haversineKm: haversineKm
  };
})(typeof window !== "undefined" ? window : this);
