"""THE canonical Budva CLOUD verdict, computed ONCE in the cloud pipeline.

Mirrors radar/verdict.py in shape and intent: a single source of truth for the
cloud conclusion that every surface RENDERS (cloud-map.html and its JS port in
docs/cloud-text.js), so wording/state never drift between Python and JS.

States:
  CLEAR              little/no cloud over the location and none incoming
  CLOUDS_APPROACHING clear now, but a cloud field is advecting in (ETA)
  PARTLY             broken/scattered cloud over the location now
  OVERCAST           solid cloud over the location now
  CLEARING           cloudy now, but a clear gap is advecting in (ETA -> sun)

`interpret(facts)` consumes the facts dict produced by clouds/interpret.py.
"""

import math

import config


# --- styling, mirrors radar/verdict.py STATE_META ---------------------------
STATE_META = {
    "OVERCAST":           {"cls": "warn", "bg": "#455a64", "fg": "#fff",    "head": "OVERCAST"},
    "CLOUDS_APPROACHING": {"cls": "warn", "bg": "#1565c0", "fg": "#fff",    "head": "CLOUDS APPROACHING"},
    "CLEARING":           {"cls": "ok",   "bg": "#42a5f5", "fg": "#fff",    "head": "CLEARING"},
    "PARTLY":             {"cls": "ok",   "bg": "#90a4ae", "fg": "#fff",    "head": "PARTLY CLOUDY"},
    "CLEAR":              {"cls": "ok",   "bg": "#ffd54f", "fg": "#3e2723", "head": "CLEAR SKY"},
}

_CARD_SR = {"N": "S", "NE": "SI", "E": "I", "SE": "JI",
            "S": "J", "SW": "JZ", "W": "Z", "NW": "SZ"}

_OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E",
             "NE": "SW", "SW": "NE", "SE": "NW", "NW": "SE"}


def _from_cardinal(facts):
    """Where the cloud comes FROM = opposite of the motion (toward) cardinal."""
    return facts.get("fromCardinal") or _OPPOSITE.get(facts.get("motionCardinal"))


def _round(x):
    """Half-up rounding — mirrors JS Math.round (Python round() is banker's)."""
    return int(math.floor(float(x) + 0.5))


def _smjer_sr(cardinal):
    return _CARD_SR.get(cardinal, cardinal) if cardinal else ""


def _pct(frac):
    if frac is None or (isinstance(frac, float) and math.isnan(frac)):
        return None
    return _round(max(0.0, min(1.0, float(frac))) * 100)


def _type_phrase(facts):
    """Human cloud descriptor, e.g. 'high thin cloud (cirrus)'. Prefer the
    precomputed cloudTypeLabel; otherwise compose from band + thickness."""
    lbl = facts.get("cloudTypeLabel")
    if lbl:
        return lbl
    band = facts.get("heightBand")
    thick = facts.get("thickness")
    if band and thick:
        return f"{band} {thick} cloud"
    if band:
        return f"{band} cloud"
    return "cloud"


def _now_level(frac):
    """clear / partly / overcast from the cloud fraction at the location."""
    c = config.CLOUDS
    if frac is None or (isinstance(frac, float) and math.isnan(frac)):
        return "clear"
    if frac <= c["frac_clear_max"]:
        return "clear"
    if frac >= c["frac_overcast_min"]:
        return "overcast"
    return "partly"


def interpret(facts):
    """facts (from clouds/interpret.py) -> {state, headline, narrative, meta}."""
    facts = facts or {}
    loc = facts.get("locationName") or "this location"
    frac = facts.get("cloudFracNow")
    # Level (clear/partly/overcast) is judged on the OPTICAL-DEPTH-weighted sky
    # cover, so thin high cirrus does not read as overcast. Coverage % is still
    # shown for context.
    sky = facts.get("skyCoverEff")
    if sky is None:
        sky = frac
    pct = _pct(frac)
    pct_txt = f" ({pct}%)" if pct is not None else ""
    level = _now_level(sky)
    thin = bool(facts.get("thinVeil"))
    typ = _type_phrase(facts)
    eta = facts.get("etaMin")
    eta_txt = f", ETA ~{_round(eta)} min" if eta is not None else ""
    from_card = _from_cardinal(facts)
    frm = f" from {from_card}" if from_card else ""
    tops = (f", tops ~{_round(facts['cloudTopHeightM'])} m"
            if facts.get("cloudTopHeightM") is not None else "")
    outlook = facts.get("sunOutlook") or ""

    if level == "clear":
        if facts.get("approaching"):
            state = "CLOUDS_APPROACHING"
            typ_appr = typ if facts.get("heightBand") else "clouds"
            narrative = f"Clear now, but {typ_appr} approaching{frm}{eta_txt}."
        elif thin:
            state = "CLEAR"
            narrative = f"Mostly clear over {loc} — {typ}{pct_txt}, sun gets through."
        else:
            state = "CLEAR"
            narrative = f"Clear sky over {loc} — no cloud incoming."
            if outlook:
                narrative += f" {outlook}"
    else:  # partly / overcast
        if facts.get("clearing"):
            state = "CLEARING"
            narrative = f"Cloudy now but clearing{pct_txt} — sun in ~{_round(eta)} min." \
                if eta is not None else f"Cloudy now but clearing{pct_txt}."
        elif level == "overcast":
            state = "OVERCAST"
            narrative = f"Overcast over {loc}{pct_txt} — {typ}{tops}."
        elif thin:
            state = "PARTLY"
            narrative = f"Hazy sun over {loc} — mostly {typ}{pct_txt}."
        else:
            state = "PARTLY"
            narrative = f"Partly cloudy over {loc}{pct_txt} — {typ}."

    meta = STATE_META[state]
    return {"state": state,
            "headline": f"{meta['head']} — {loc}",
            "narrative": narrative,
            "meta": meta}


def serbian_line(facts, res):
    """One-sentence Serbian status line: {text, bold, color, weight}."""
    facts = facts or {}
    state = res["state"]
    pct = _pct(facts.get("cloudFracNow"))
    pct_txt = f" ({pct}%)" if pct is not None else ""
    eta = facts.get("etaMin")
    typ = _type_phrase(facts)

    if state == "CLOUDS_APPROACHING":
        eta_txt = f", ~{_round(eta)} min" if eta is not None else ""
        from_card = _smjer_sr(_from_cardinal(facts))
        frm = f" sa {from_card}" if from_card else ""
        return {"text": f"oblaci se približavaju Budvi{frm}{eta_txt}",
                "bold": "oblaci se približavaju Budvi", "color": "#1565c0", "weight": 700}
    if state == "CLEARING":
        eta_txt = f" za ~{_round(eta)} min" if eta is not None else ""
        return {"text": f"razvedrava se nad Budvom — sunce{eta_txt}",
                "bold": "razvedrava se nad Budvom", "color": "#1565c0", "weight": 600}
    if state == "OVERCAST":
        return {"text": f"oblačno nad Budvom{pct_txt} — {typ}",
                "bold": "oblačno nad Budvom", "color": "#455a64", "weight": 700}
    if state == "PARTLY":
        if facts.get("thinVeil"):
            return {"text": f"sunce kroz tanak visoki oblak nad Budvom{pct_txt}",
                    "bold": "sunce kroz tanak visoki oblak", "color": "#f9a825", "weight": 600}
        return {"text": f"djelimično oblačno nad Budvom{pct_txt}",
                "bold": "djelimično oblačno nad Budvom", "color": "#607d8b", "weight": 600}
    # CLEAR
    if facts.get("thinVeil"):
        return {"text": f"pretežno vedro nad Budvom — tanak visoki oblak, sunce probija{pct_txt}",
                "bold": "pretežno vedro nad Budvom", "color": "#f9a825", "weight": 600}
    return {"text": "vedro nad Budvom", "bold": "vedro nad Budvom",
            "color": "#f9a825", "weight": 600}


def cloud_verdict(status):
    """Canonical verdict dict shipped as summary.cloud_verdict. Reads the
    precomputed facts at the location; returns None when no usable data."""
    src = status.get("source") or {}
    facts = status.get("facts")
    if not src.get("ok") or not facts:
        return None
    res = interpret(facts)
    sr = serbian_line(facts, res)
    return {
        "state": res["state"],
        "headline": res["headline"],
        "narrative": res["narrative"],
        "style": res["meta"],
        "line_sr": sr["text"],
        "line_sr_bold": sr["bold"],
        "color_sr": sr["color"],
        "weight_sr": sr["weight"],
        "sun_outlook": facts.get("sunOutlook") or "",
        "facts": {k: facts.get(k) for k in
                  ("cloudFracNow", "cloudAtLocation", "approaching", "clearing",
                   "etaMin", "heightBand", "thickness", "phase", "cloudTypeLabel",
                   "cloudTopHeightM", "cloudTopTempC")},
    }
