"""Parity test: radar/verdict.py (Python, authoritative — computes the canonical
verdict every page renders) must match the JS interpreter (skala-text.js +
skala-sections.js fallback path) for the same status data.

The verdict is now BINARY (RAINING vs NO_RAIN — current state only; the forecast
moved to SKALA NOWCAST), so every case must collapse to one of those two states,
identically in Python and JS.

Part A: the real, current docs/data.js.
Part B: synthetic sources across the old scenarios (raining, severe overhead,
        approaching, bypassing, far echo, clear) — all must now map to
        RAINING/NO_RAIN, and Python must agree with the JS interpreter.

Run from repo root:  python tests/test_verdict_parity.py
Exit 0 = parity holds; exit 1 = mismatch.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import verdict  # noqa: E402

RUNNER = Path(__file__).parent / "_run_skala.js"


def run_js(status):
    fd, name = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(status, f, default=str)
        out = subprocess.run(["node", str(RUNNER), name],
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            raise RuntimeError("node runner failed:\n" + out.stderr)
        return json.loads(out.stdout)
    finally:
        os.unlink(name)


def compare(label, status, fails):
    py = verdict.budva_verdict(status)
    # strip any precomputed verdicts so the JS runs its own computation path
    js_in = json.loads(json.dumps(status, default=str))
    (js_in.get("summary") or {}).pop("budva_verdict", None)
    for s in (js_in.get("sources") or {}).values():
        if isinstance(s, dict):
            s.pop("verdict", None)
    js = run_js(js_in)
    for key in ("state", "headline", "narrative"):
        a, b = (py or {}).get(key), (js or {}).get(key)
        if a != b:
            fails.append(f"[{label}] {key}: py={a!r} vs js={b!r}")
    # the verdict is binary now — no case may produce anything but these two
    st = (py or {}).get("state")
    if st is not None and st not in ("RAINING", "NO_RAIN"):
        fails.append(f"[{label}] non-binary state: {st!r}")


def synth_source(rain_at_loc=False, approaching=False, any_rain=True,
                 cw=(12.0, "NW", 27.5), dom=None, eta=25.0, motion_card="SE",
                 any_echo=True):
    if cw:
        rings = [{"radius_km": 25, "n_wet": 12, "n_echo": 30,
                  "closest_wet_km": cw[0],
                  "closest_wet_bearing_cardinal": cw[1],
                  "closest_wet_dbz": cw[2]}]
    else:
        rings = [{"radius_km": 25, "n_wet": 0, "n_echo": 30 if any_echo else 0}]
    return {
        "ok": True,
        "rings": rings,
        "motion": {"direction_cardinal": motion_card},
        "approaching": {
            "is_approaching": approaching,
            "rain_at_location": rain_at_loc,
            "any_rain_within_radii": any_rain,
            "closest_rain_km": cw[0] if cw else None,
            "closest_rain_intensity_dbz": cw[2] if cw else None,
            "closest_rain_bearing_cardinal": cw[1] if cw else None,
            "closest_rain_intensity_label": None,
            "eta_minutes": eta,
            "motion_direction_cardinal": motion_card,
            "nowcast_details": {"dominant": dom},
        },
    }


def wrap(src):
    return {"location": {"name": "Budva"}, "sources": {"dhmz": src}, "summary": {}}


def main():
    fails = []

    # ---- Part A: the real current data.js -------------------------------
    data_js = (ROOT / "docs" / "data.js").read_text(encoding="utf-8")
    m = re.search(r"window\.RADAR_DATA\s*=\s*(\{.*\});\s*$", data_js, re.S)
    if m:
        compare("real data.js", json.loads(m.group(1)), fails)
        print("Part A: real data.js compared")
    else:
        print("Part A: SKIPPED (couldn't parse data.js)")

    # ---- Part B: synthetic branch coverage ------------------------------
    dom_severe = {"dist_km": 40.0, "max_dbz": 57.5, "bearing_cardinal": "W",
                  "eta_minutes": 35.0, "intensity_label": "extreme (hail core)"}
    # Same severe cell but CPA-classified HIT -> SEVERE fires (PDF Part E gate).
    dom_severe_hit = {"dist_km": 30.0, "max_dbz": 57.5, "bearing_cardinal": "W",
                      "eta_minutes": 25.0, "intensity_label": "extreme (hail core)",
                      "classification": "HIT"}
    # Severe cell that BYPASSes -> stays APPROACHING/regional, NOT a point SEVERE.
    dom_severe_bypass = {"dist_km": 30.0, "max_dbz": 57.5, "bearing_cardinal": "W",
                         "eta_minutes": 25.0, "intensity_label": "extreme (hail core)",
                         "classification": "BYPASS"}
    dom_mod = {"dist_km": 60.0, "max_dbz": 37.5, "bearing_cardinal": "NW",
               "eta_minutes": 55.0, "intensity_label": "moderate rain"}
    dom_far = {"dist_km": 209.0, "max_dbz": 67.5, "bearing_cardinal": "SE",
               "eta_minutes": 103.0, "intensity_label": "extreme (hail core)"}
    cases = {
        "raining": synth_source(rain_at_loc=True, cw=(0.4, "N", 32.5)),
        "severe overhead": synth_source(rain_at_loc=True, cw=(0.2, "N", 52.5)),
        "severe approaching": synth_source(approaching=True, dom=dom_severe,
                                           cw=(30.0, "W", 32.5)),
        "severe approaching HIT": synth_source(approaching=True, dom=dom_severe_hit,
                                               cw=(30.0, "W", 32.5), eta=25.0),
        "severe approaching BYPASS": synth_source(approaching=True, dom=dom_severe_bypass,
                                                  cw=(30.0, "W", 32.5), eta=25.0),
        "approaching": synth_source(approaching=True, dom=dom_mod,
                                    cw=(45.0, "NW", 27.5), eta=50.0),
        "bypassing": synth_source(cw=(8.0, "NE", 24.5)),
        "no rain far echo": synth_source(cw=(80.0, "NW", 22.5)),
        "vicinity bound (dom 209 km)": synth_source(approaching=True,
                                                    dom=dom_far, cw=None,
                                                    any_rain=False),
        "clear echo only": synth_source(cw=None, any_rain=False),
    }
    for label, src in cases.items():
        compare(label, wrap(src), fails)
    print(f"Part B: {len(cases)} synthetic cases compared")

    if fails:
        print("\nFAIL — verdict parity mismatches:")
        for f in fails:
            print("  " + f)
        return 1
    print("\nPASS — Python verdict matches the JS interpreter on all cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
