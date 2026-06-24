// cloud-sections.js — shared renderers for cloud-map.html. The canonical cloud
// verdict comes from CLOUD_DATA.summary.cloud_verdict (computed in Python); this
// only RENDERS it (and falls back to CLOUD_TEXT for clicked points).
(function (global) {
  function el(id) { return document.getElementById(id); }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]; }); }

  // Prefer the precomputed canonical verdict; else compute from facts in JS.
  function cloudHeadline(data) {
    data = data || {};
    var v = (data.summary || {}).cloud_verdict;
    if (v && v.state && v.headline) {
      return { state: v.state, headline: v.headline, narrative: v.narrative,
               meta: v.style || (global.CLOUD_TEXT && global.CLOUD_TEXT.STATE_META[v.state]) || {},
               line_sr: v.line_sr, sun_outlook: v.sun_outlook, sun: v.sun };
    }
    if (global.CLOUD_TEXT && data.facts) {
      var res = global.CLOUD_TEXT.interpret(data.facts, data.params);
      var sr = global.CLOUD_TEXT.serbianLine(data.facts, res);
      return { state: res.state, headline: res.headline, narrative: res.narrative,
               meta: res.meta, line_sr: sr.text, sun_outlook: data.facts.sunOutlook || "",
               sun: global.CLOUD_TEXT.sunDescriptor(data.facts) };
    }
    return { state: "CLEAR", headline: "—", narrative: "nema podataka", meta: {} };
  }

  function renderBanner(node, h) {
    var m = h.meta || {};
    node.style.background = m.bg || "#eee";
    node.style.color = m.fg || "#222";
    node.innerHTML =
      '<div style="font-weight:700;font-size:16px;letter-spacing:.3px;">' + esc(h.headline) + "</div>" +
      '<div style="margin-top:5px;font-size:14px;">' + esc(h.narrative) + "</div>";
  }

  function _row(k, v) { return v == null || v === "" ? "" : "<tr><td style='color:#666;padding:2px 10px 2px 0;'>" + esc(k) + "</td><td>" + esc(v) + "</td></tr>"; }

  // Sun/shade label (mirror clouds/verdict.py sun_descriptor wording, Serbian).
  function _sunLabel(facts) {
    if (facts.isNight) return "noć — IR detekcija (bez procjene sunca)";
    var map = { sunny: "sunce probija", dimmed: "sunce prigušeno", blocked: "sunce zaklonjeno" };
    var s = facts.sunState ? map[facts.sunState] : null;
    if (!s) return null;
    // Show the GLOBAL-irradiance CMF (the PDF's "is it sunny" number) — not the
    // direct-beam transmittance, which reads a few % even when the scene is sunny.
    if (facts.cmf != null) s += " (CMF≈" + Math.round(facts.cmf * 100) + "%)";
    else if (facts.sunTransmittance != null) s += " (T≈" + Math.round(facts.sunTransmittance * 100) + "%)";
    return s;
  }

  function renderFacts(node, facts) {
    facts = facts || {};
    // PRESENCE (is there cloud — thin cirrus counts) vs SUN-BLOCKING are two
    // separate numbers (the PDF's core point), shown on separate rows.
    var pct = facts.cloudFracNow == null ? null : Math.round(facts.cloudFracNow * 100) + "%";
    var block = facts.skyCoverEff != null ? facts.skyCoverEff
              : (facts.opaqueFracNow != null ? facts.opaqueFracNow : null);
    var blockPct = block == null ? null : Math.round(block * 100) + "%";
    var rows = [
      _row("Oblačnost (prisustvo)", pct),
      _row("Sunce", _sunLabel(facts)),
      _row("Zaklanja sunce", blockPct),
      _row("Tip", facts.cloudTypeLabel),
      _row("Visina vrha", facts.cloudTopHeightM != null ? Math.round(facts.cloudTopHeightM) + " m (" + (facts.heightBand || "?") + ")" : null),
      _row("Debljina", facts.thickness ? facts.thickness + (facts.opticalThickness != null ? " (COT " + facts.opticalThickness + ")" : "") : null),
      _row("Temp. vrha", facts.cloudTopTempC != null ? facts.cloudTopTempC + " °C" : null),
      _row("Faza", facts.phase),
      _row("Kretanje", facts.motionCardinal ? "ka " + facts.motionCardinal + (facts.motionSpeedKmh != null ? " @ " + facts.motionSpeedKmh + " km/h" : "") : null),
      _row("Sunčev zenit", facts.szaDeg != null ? Math.round(facts.szaDeg) + "°" : null),
      _row("Izgledi (0–2 h)", facts.sunOutlook)
    ].join("");
    node.innerHTML = "<table style='font-size:13px;border-collapse:collapse;'>" + rows + "</table>";
  }

  function renderRings(node, rings) {
    rings = rings || [];
    node.innerHTML = rings.map(function (r) {
      var f = r.cloud_fraction == null ? "—" : Math.round(r.cloud_fraction * 100) + "%";
      return "<li>" + r.radius_km + " km: <b>" + f + "</b> oblačnost</li>";
    }).join("");
  }

  // 2–48 h NWP cloud-cover band (modeled — kept visually separate from the
  // observed satellite verdict). Reads weather-forecast's hourly_forecast.
  function renderNwp(node, nwp) {
    var hours = (nwp && nwp.hourly_forecast) || [];
    if (!hours.length) { node.innerHTML = "<span style='color:#999;'>NWP izgledi nedostupni.</span>"; return; }
    var slice = hours.slice(0, 48);
    var bars = slice.map(function (h, i) {
      var cc = h.cloud_cover == null ? 0 : h.cloud_cover;
      var shade = Math.round(220 - cc * 1.4); // more cloud -> darker grey
      var label = (i % 6 === 0) ? (h.hour != null ? h.hour + "h" : "") : "";
      return "<div title='" + esc((h.datetime || "") + ": " + cc + "%") + "' style='flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:60px;'>" +
        "<div style='height:" + cc + "%;background:rgb(" + shade + "," + shade + "," + (shade + 8) + ");border-top:1px solid #b0bec5;'></div>" +
        "<div style='font-size:9px;color:#999;text-align:center;height:12px;'>" + label + "</div></div>";
    }).join("");
    node.innerHTML =
      "<div style='display:flex;gap:1px;align-items:flex-end;'>" + bars + "</div>" +
      "<div style='font-size:11px;color:#888;margin-top:4px;'>Modelirana oblačnost (NWP, sljedeća ~48 h) — odvojeno od satelitskog osmatranja gore.</div>";
  }

  global.CLOUD_SECTIONS = {
    cloudHeadline: cloudHeadline, renderBanner: renderBanner, renderFacts: renderFacts,
    renderRings: renderRings, renderNwp: renderNwp, _el: el
  };
})(typeof window !== "undefined" ? window : this);
