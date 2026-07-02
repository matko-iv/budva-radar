"""Parity test: clouds/verdict.py (Python, authoritative) must match the browser
port docs/cloud-text.js for the SAME facts — including the new two-axis / night
branches the sun/shade work added (sun gets through, sun blocked, IR-only at night).

Run from repo root:  python tests/test_cloud_text_parity.py
Exit 0 = parity holds; exit 1 = mismatch. Requires node.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import verdict  # noqa: E402

RUNNER = Path(__file__).parent / "_run_cloud_text.js"


def run_js(facts):
    fd, name = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(facts, f, default=str)
        out = subprocess.run(["node", str(RUNNER), name],
                             capture_output=True, text=True, encoding="utf-8")
        if out.returncode != 0:
            raise RuntimeError("node runner failed:\n" + out.stderr)
        return json.loads(out.stdout)
    finally:
        os.unlink(name)


def facts(**over):
    f = {
        "locationName": "Budva", "cloudFracNow": 0.05, "skyCoverEff": 0.05,
        "thinVeil": False, "approaching": False, "clearing": False, "etaMin": None,
        "motionCardinal": "NW", "heightBand": None, "thickness": None, "phase": None,
        "cloudTypeLabel": None, "cloudTopHeightM": None, "sunOutlook": "",
        "isNight": False, "sunState": "sunny",
    }
    f.update(over)
    return f


CASES = {
    "clear day": facts(),
    "thin cirrus clear (sunny)": facts(
        cloudFracNow=0.9, skyCoverEff=0.05, thinVeil=True, heightBand="high",
        thickness="thin", phase="ice", cloudTypeLabel="high thin cloud (cirrus)",
        sunState="sunny"),
    "overcast blocked": facts(
        cloudFracNow=0.95, skyCoverEff=0.95, heightBand="low", thickness="thick",
        cloudTopHeightM=1200.0, cloudTypeLabel="low thick cloud (stratus)",
        sunState="blocked"),
    "partly dimmed": facts(cloudFracNow=0.5, skyCoverEff=0.5, heightBand="mid",
                           sunState="dimmed"),
    "mostly sunny (sun through)": facts(
        cloudFracNow=0.9, skyCoverEff=0.31, heightBand="high", thickness="thick",
        phase="ice", cloudTypeLabel="high thick cloud (cirrostratus)",
        sunState="sunny"),
    "approaching": facts(approaching=True, etaMin=40, heightBand="high",
                         thickness="thin", cloudTypeLabel="high thin cloud (cirrus)"),
    "clearing": facts(cloudFracNow=0.9, skyCoverEff=0.9, clearing=True, etaMin=30,
                      heightBand="low", thickness="thick", sunState="dimmed"),
    "night overcast": facts(
        cloudFracNow=0.95, skyCoverEff=0.95, heightBand="low", thickness="thick",
        cloudTopHeightM=1200.0, cloudTypeLabel="low thick cloud (stratus)",
        isNight=True, sunState=None),
    "night thin veil": facts(
        cloudFracNow=0.9, skyCoverEff=0.05, thinVeil=True, heightBand="high",
        thickness="thin", cloudTypeLabel="high thin cloud (cirrus)",
        isNight=True, sunState=None),
}


def main():
    fails = []
    for label, f in CASES.items():
        res = verdict.interpret(f)
        sr = verdict.serbian_line(f, res)
        sun = verdict.sun_descriptor(f)
        py = {"state": res["state"], "headline": res["headline"],
              "narrative": res["narrative"], "line_sr": sr["text"], "sun": sun}
        js = run_js(f)
        for key in ("state", "headline", "narrative", "line_sr", "sun"):
            if py[key] != js.get(key):
                fails.append(f"[{label}] {key}: py={py[key]!r} vs js={js.get(key)!r}")
        print(("PASS  " if not any(label in x for x in fails) else "FAIL  ") + label)

    if fails:
        print("\nFAIL — cloud verdict parity mismatches:")
        for x in fails:
            print("  " + x)
        return 1
    print("\nPASS — Python cloud verdict matches the JS port on all cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
