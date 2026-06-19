// cloud-text.js — browser port of clouds/verdict.py (state machine + wording).
// THE Python cloud_verdict is the source of truth; this is the fallback + the
// per-clicked-point path on cloud-map.html. Keep in sync with clouds/verdict.py.
(function (global) {
  var STATE_META = {
    OVERCAST:           { cls: "warn", bg: "#455a64", fg: "#fff",    head: "OVERCAST" },
    CLOUDS_APPROACHING: { cls: "warn", bg: "#1565c0", fg: "#fff",    head: "CLOUDS APPROACHING" },
    CLEARING:           { cls: "ok",   bg: "#42a5f5", fg: "#fff",    head: "CLEARING" },
    PARTLY:             { cls: "ok",   bg: "#90a4ae", fg: "#fff",    head: "PARTLY CLOUDY" },
    CLEAR:              { cls: "ok",   bg: "#ffd54f", fg: "#3e2723", head: "CLEAR SKY" }
  };
  var CARD_SR = { N: "S", NE: "SI", E: "I", SE: "JI", S: "J", SW: "JZ", W: "Z", NW: "SZ" };
  var OPP = { N: "S", S: "N", E: "W", W: "E", NE: "SW", SW: "NE", SE: "NW", NW: "SE" };
  var DEFAULTS = { frac_clear_max: 0.2, frac_overcast_min: 0.8 };

  function rnd(x) { return Math.round(x); }
  function pct(f) { return (f == null || isNaN(f)) ? null : Math.round(Math.min(1, Math.max(0, f)) * 100); }
  function fromCard(facts) { return facts.fromCardinal || OPP[facts.motionCardinal]; }
  function smjer(c) { return c ? (CARD_SR[c] || c) : ""; }

  function typePhrase(facts) {
    if (facts.cloudTypeLabel) return facts.cloudTypeLabel;
    if (facts.heightBand && facts.thickness) return facts.heightBand + " " + facts.thickness + " cloud";
    if (facts.heightBand) return facts.heightBand + " cloud";
    return "cloud";
  }

  function nowLevel(frac, p) {
    if (frac == null || isNaN(frac)) return "clear";
    if (frac <= p.frac_clear_max) return "clear";
    if (frac >= p.frac_overcast_min) return "overcast";
    return "partly";
  }

  function interpret(facts, params) {
    facts = facts || {}; var p = params || DEFAULTS;
    var loc = facts.locationName || "this location";
    var frac = facts.cloudFracNow, pc = pct(frac);
    var pctTxt = pc != null ? " (" + pc + "%)" : "";
    // Level judged on optical-depth-weighted sky cover (thin cirrus != overcast).
    var sky = facts.skyCoverEff != null ? facts.skyCoverEff : frac;
    var level = nowLevel(sky, p), thin = !!facts.thinVeil, typ = typePhrase(facts);
    var eta = facts.etaMin, etaTxt = eta != null ? ", ETA ~" + rnd(eta) + " min" : "";
    var fc = fromCard(facts), frm = fc ? " from " + fc : "";
    var tops = facts.cloudTopHeightM != null ? ", tops ~" + rnd(facts.cloudTopHeightM) + " m" : "";
    var outlook = facts.sunOutlook || "";
    var state, narrative;

    if (level === "clear") {
      if (facts.approaching) {
        state = "CLOUDS_APPROACHING";
        var ta = facts.heightBand ? typ : "clouds";
        narrative = "Clear now, but " + ta + " approaching" + frm + etaTxt + ".";
      } else if (thin) {
        state = "CLEAR";
        narrative = "Mostly clear over " + loc + " — " + typ + pctTxt + ", sun gets through.";
      } else {
        state = "CLEAR";
        narrative = "Clear sky over " + loc + " — no cloud incoming.";
        if (outlook) narrative += " " + outlook;
      }
    } else {
      if (facts.clearing) {
        state = "CLEARING";
        narrative = eta != null
          ? "Cloudy now but clearing" + pctTxt + " — sun in ~" + rnd(eta) + " min."
          : "Cloudy now but clearing" + pctTxt + ".";
      } else if (level === "overcast") {
        state = "OVERCAST";
        narrative = "Overcast over " + loc + pctTxt + " — " + typ + tops + ".";
      } else if (thin) {
        state = "PARTLY";
        narrative = "Hazy sun over " + loc + " — mostly " + typ + pctTxt + ".";
      } else {
        state = "PARTLY";
        narrative = "Partly cloudy over " + loc + pctTxt + " — " + typ + ".";
      }
    }
    var meta = STATE_META[state];
    return { state: state, headline: meta.head + " — " + loc, narrative: narrative, meta: meta };
  }

  function serbianLine(facts, res) {
    facts = facts || {};
    var state = res.state, pc = pct(facts.cloudFracNow);
    var pctTxt = pc != null ? " (" + pc + "%)" : "", eta = facts.etaMin, typ = typePhrase(facts);
    if (state === "CLOUDS_APPROACHING") {
      var etaTxt = eta != null ? ", ~" + rnd(eta) + " min" : "";
      var fc = smjer(fromCard(facts)), frm = fc ? " sa " + fc : "";
      return { text: "oblaci se približavaju Budvi" + frm + etaTxt, bold: "oblaci se približavaju Budvi", color: "#1565c0", weight: 700 };
    }
    if (state === "CLEARING") {
      var e2 = eta != null ? " za ~" + rnd(eta) + " min" : "";
      return { text: "razvedrava se nad Budvom — sunce" + e2, bold: "razvedrava se nad Budvom", color: "#1565c0", weight: 600 };
    }
    if (state === "OVERCAST") return { text: "oblačno nad Budvom" + pctTxt + " — " + typ, bold: "oblačno nad Budvom", color: "#455a64", weight: 700 };
    if (state === "PARTLY") {
      if (facts.thinVeil) return { text: "sunce kroz tanak visoki oblak nad Budvom" + pctTxt, bold: "sunce kroz tanak visoki oblak", color: "#f9a825", weight: 600 };
      return { text: "djelimično oblačno nad Budvom" + pctTxt, bold: "djelimično oblačno nad Budvom", color: "#607d8b", weight: 600 };
    }
    if (facts.thinVeil) return { text: "pretežno vedro nad Budvom — tanak visoki oblak, sunce probija" + pctTxt, bold: "pretežno vedro nad Budvom", color: "#f9a825", weight: 600 };
    return { text: "vedro nad Budvom", bold: "vedro nad Budvom", color: "#f9a825", weight: 600 };
  }

  global.CLOUD_TEXT = {
    STATE_META: STATE_META, interpret: interpret, serbianLine: serbianLine,
    typePhrase: typePhrase, nowLevel: nowLevel
  };
})(typeof window !== "undefined" ? window : this);
