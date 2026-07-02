"""Canonical Budva radar verdict, computed once in the pipeline.

The pages used to re-derive the conclusion in their own JS and drifted apart.
This is a faithful port of skala-sections.js:factsFromSource +
skala-text.js:SKALA.interpret plus the Serbian one-liner from forecast.html;
the pipeline ships it as summary.budva_verdict and the pages only render it.
tests/test_verdict_parity.py replays the JS interpreter via node against this
port, so keep wording and state logic in lockstep with the JS.
"""

import math


SKALA_VICINITY_KM = 20
SEVERE_DBZ = 50
VICINITY_MAX_KM = 150  # max SAMPLE_RADII_KM, mirrors skala-sections.js
# Deterministic cell-arrival skill is ~30-60 min; ETAs beyond this are
# flagged probabilistic. Mirrored in docs/skala-text.js.
DETERMINISTIC_ETA_MAX_MIN = 30

STATE_META = {
    "SEVERE":      {"cls": "severe", "bg": "#6a1b9a", "fg": "#fff",    "head": "SEVERE STORM"},
    "RAINING":     {"cls": "warn",   "bg": "#1565c0", "fg": "#fff",    "head": "RAINING NOW"},
    "APPROACHING": {"cls": "warn",   "bg": "#c62828", "fg": "#fff",    "head": "RAIN APPROACHING"},
    "BYPASSING":   {"cls": "ok",     "bg": "#9e9e9e", "fg": "#fff",    "head": "RAIN NEARBY — BYPASSING"},
    "NO_RAIN":     {"cls": "ok",     "bg": "#a5d6a7", "fg": "#1b3a1c", "head": "NO RAIN"},
}


def _round(x):
    """Half-up like JS Math.round; Python's banker's round broke parity."""
    return int(math.floor(x + 0.5))


def _intensity(dbz):
    if dbz is None or (isinstance(dbz, float) and math.isnan(dbz)):
        return "rain"
    if dbz < 25:
        return "light rain"
    if dbz < 40:
        return "moderate rain"
    if dbz < 50:
        return "heavy rain"
    return "hail"


def _intenzitet_sr(dbz):
    if dbz is None or (isinstance(dbz, float) and math.isnan(dbz)):
        return "kiša"
    if dbz < 25:
        return "slaba kiša"
    if dbz < 40:
        return "umjerena kiša"
    if dbz < 50:
        return "jaka kiša"
    return "grad"


_CARD_SR = {"N": "S", "NE": "SI", "E": "I", "SE": "JI",
            "S": "J", "SW": "JZ", "W": "Z", "NW": "SZ"}


def _smjer_sr(cardinal):
    return _CARD_SR.get(cardinal, cardinal) if cardinal else ""


def _eta_text(eta, prob=" (probabilistic)"):
    """ETA clause; beyond the deterministic horizon it is flagged probabilistic."""
    if eta is None:
        return ""
    r = _round(eta)
    tail = prob if r > DETERMINISTIC_ETA_MAX_MIN else ""
    return f", ETA ~{r} min{tail}"


def _fmt_km(km):
    if km is None:
        return "?"
    if km < 1:
        return f"{km:.2f}"
    if km < 10:
        return f"{km:.1f}"
    return str(_round(km))


def _closest_wet(src):
    """Closest wet pixel from the rings (mirrors skala-sections.js closestWet)."""
    best = None
    for r in (src.get("rings") or []):
        km = r.get("closest_wet_km")
        if km is not None and (best is None or km < best["km"]):
            best = {"km": km, "cardinal": r.get("closest_wet_bearing_cardinal"),
                    "dbz": r.get("closest_wet_dbz")}
    return best


def facts_from_source(src, loc_name):
    """Normalized facts for one source (mirrors skala-sections.js factsFromSource)."""
    src = src or {}
    app = src.get("approaching") or {}
    rings = src.get("rings") or []
    mot = src.get("motion") or {}

    dom = (app.get("nowcast_details") or {}).get("dominant")
    dom_in_range = bool(dom and dom.get("dist_km") is not None
                        and dom["dist_km"] <= VICINITY_MAX_KM)
    if dom_in_range:
        threat = {"dbz": dom.get("max_dbz"), "km": dom.get("dist_km"),
                  "cardinal": dom.get("bearing_cardinal"),
                  "eta": dom.get("eta_minutes"), "label": dom.get("intensity_label"),
                  # SEVERE is gated on this being a HIT; a bypassing or
                  # receding cell never raises a point severe alert.
                  "cpaClass": dom.get("classification")}
    elif app.get("closest_rain_km") is not None and app["closest_rain_km"] <= VICINITY_MAX_KM:
        threat = {"dbz": app.get("closest_rain_intensity_dbz"), "km": app.get("closest_rain_km"),
                  "cardinal": app.get("closest_rain_bearing_cardinal"),
                  "eta": app.get("eta_minutes"),
                  "label": app.get("closest_rain_intensity_label"),
                  "cpaClass": None}
    else:
        threat = None

    cw = _closest_wet(src)
    approaching = bool(app.get("is_approaching")) and (dom_in_range or cw is not None)
    return {
        "locationName": loc_name,
        "rainAtLocation": bool(app.get("rain_at_location")),
        "approaching": approaching,
        "anyRain": bool(app.get("any_rain_within_radii")),
        "anyWet": any((r.get("n_wet") or 0) > 0 for r in rings),
        "anyEcho": any((r.get("n_echo") or 0) > 0 for r in rings),
        "km": cw["km"] if cw else app.get("closest_rain_km"),
        "cardinal": cw["cardinal"] if cw else app.get("closest_rain_bearing_cardinal"),
        "dbz": cw["dbz"] if cw else app.get("closest_rain_intensity_dbz"),
        "motionCardinal": app.get("motion_direction_cardinal") or mot.get("direction_cardinal"),
        "eta": app.get("eta_minutes"),
        "threat": threat,
    }


def interpret(facts):
    """Binary verdict: raining at the location right now or not. Forecasting
    (approaching / ETA / severe-incoming) belongs to SKALA NOWCAST. Must stay
    identical to skala-text.js skalaInterpret; the parity test enforces it."""
    facts = facts or {}
    loc = facts.get("locationName") or "this location"
    dbz = facts.get("dbz")
    km = facts.get("km")
    intensity = _intensity(dbz)
    dbz_txt = f" ({_round(dbz)} dBZ)" if dbz is not None else ""
    where = f"~{_fmt_km(km)} km" + (f" {facts['cardinal']}" if facts.get("cardinal") else "")

    if facts.get("rainAtLocation"):
        state = "RAINING"
        narrative = f"Raining now — {intensity}{dbz_txt}."
    else:
        state = "NO_RAIN"
        if facts.get("anyRain") and km is not None:
            narrative = f"No rain at {loc} (nearest echo {where})."
        elif facts.get("anyWet"):
            narrative = "Scattered radar echoes below the rain threshold — not falling."
        elif facts.get("anyEcho"):
            narrative = "Only weak echo on the radar — likely noise, not rain."
        else:
            narrative = "No rain on the radar within 150 km."

    meta = STATE_META[state]
    return {"state": state,
            "headline": f"{meta['head']} — {loc}",
            "narrative": narrative,
            "meta": meta}


def serbian_line(facts, res):
    """One-sentence Serbian status line (pada / ne pada u Budvi). Returns
    {text, bold, color, weight}; the page adds its own prefix and data-age
    suffix. Not parity-checked, so wording may differ from the JS."""
    dbz = facts.get("dbz")
    km = facts.get("km")
    if res["state"] == "RAINING":
        if dbz is not None and dbz >= SEVERE_DBZ:
            return {"text": f"pada jaka kiša / grad u Budvi ({_round(dbz)} dBZ)",
                    "bold": "pada jaka kiša / grad u Budvi", "color": "#6a1b9a", "weight": 700}
        return {"text": f"pada kiša u Budvi — {_intenzitet_sr(dbz)}",
                "bold": "pada kiša u Budvi", "color": "#1565c0", "weight": 700}
    if facts.get("anyRain") and km is not None:
        return {"text": (f"trenutno ne pada u Budvi (najbliža jeka ~{_fmt_km(km)} km "
                         f"{_smjer_sr(facts.get('cardinal'))})"),
                "bold": None, "color": "#2e7d32", "weight": 400}
    return {"text": "trenutno ne pada u Budvi", "bold": None,
            "color": "#2e7d32", "weight": 400}


def budva_verdict(status):
    """Canonical verdict from the DHMZ source (falling back to any usable
    source). Returns the summary.budva_verdict dict, or None."""
    sources = status.get("sources") or {}
    loc = ((status.get("location") or {}).get("name")) or "Budva"
    src_id = "dhmz" if (sources.get("dhmz") or {}).get("ok") else next(
        (sid for sid, s in sources.items() if (s or {}).get("ok")), None)
    if src_id is None:
        return None
    facts = facts_from_source(sources[src_id], loc)
    res = interpret(facts)
    sr = serbian_line(facts, res)
    return {
        "source": src_id,
        "state": res["state"],
        "headline": res["headline"],
        "narrative": res["narrative"],
        "style": res["meta"],
        "line_sr": sr["text"],
        "line_sr_bold": sr["bold"],
        "color_sr": sr["color"],
        "weight_sr": sr["weight"],
        "facts": {k: facts.get(k) for k in
                  ("rainAtLocation", "approaching", "anyRain", "km", "dbz", "eta")},
    }
