"""Unit + branch-coverage test for clouds/verdict.py — the canonical cloud
verdict state machine (CLEAR / PARTLY / OVERCAST / CLOUDS_APPROACHING /
CLEARING). Synthetic facts -> expected state + wording, so the Python verdict
and the browser-side JS port (docs/cloud-text.js) can't silently drift.

Run from repo root:  python tests/test_cloud_verdict.py   (exit 0 = pass)
Also discoverable by pytest (test_* functions).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import verdict  # noqa: E402


def facts(**over):
    """Default = clear sky, nothing incoming; override per case."""
    f = {
        "locationName": "Budva",
        "cloudFracNow": 0.05,
        "cloudAtLocation": False,
        "approaching": False,
        "clearing": False,
        "etaMin": None,
        "motionCardinal": "NW",
        "cloudTopHeightM": None,
        "cloudTopTempC": None,
        "heightBand": None,
        "opticalThickness": None,
        "thickness": None,
        "phase": None,
        "cloudTypeLabel": None,
        "sunOutlook": "",
    }
    f.update(over)
    return f


CASES = {
    "clear": (facts(), "CLEAR"),
    "approaching": (
        facts(cloudFracNow=0.05, approaching=True, etaMin=40, motionCardinal="NW",
              heightBand="high", thickness="thin", phase="ice",
              cloudTypeLabel="high thin cloud (cirrus)"),
        "CLOUDS_APPROACHING"),
    "partly": (facts(cloudFracNow=0.5, cloudAtLocation=True, heightBand="mid"),
               "PARTLY"),
    "overcast": (
        facts(cloudFracNow=0.95, cloudAtLocation=True, heightBand="low",
              thickness="thick", cloudTopHeightM=1200.0, cloudTypeLabel="low thick cloud (stratus)"),
        "OVERCAST"),
    "clearing": (
        facts(cloudFracNow=0.9, cloudAtLocation=True, clearing=True, etaMin=30,
              heightBand="low", thickness="thick"),
        "CLEARING"),
    # high coverage but optically THIN cirrus -> stays CLEAR ("sun gets through")
    "thin_veil": (
        facts(cloudFracNow=0.7, skyCoverEff=0.12, thinVeil=True, cloudAtLocation=True,
              heightBand="high", thickness="thin", phase="ice",
              cloudTypeLabel="high thin cloud (cirrus)"),
        "CLEAR"),
}


def test_states():
    for label, (f, want) in CASES.items():
        res = verdict.interpret(f)
        assert res["state"] == want, f"[{label}] state {res['state']!r} != {want!r}"
        assert res["headline"], f"[{label}] empty headline"
        assert "Budva" in res["headline"], f"[{label}] loc missing from headline"
        assert res["narrative"], f"[{label}] empty narrative"
        assert res["meta"] and res["meta"].get("head"), f"[{label}] no style meta"


def test_eta_in_narrative():
    appr = verdict.interpret(CASES["approaching"][0])
    assert "40" in appr["narrative"], f"approaching ETA missing: {appr['narrative']!r}"
    clr = verdict.interpret(CASES["clearing"][0])
    assert "30" in clr["narrative"], f"clearing ETA missing: {clr['narrative']!r}"


def test_serbian_line():
    want_sub = {
        "clear": "vedro",
        "approaching": "približava",
        "partly": "djelimično",
        "overcast": "oblačno",
        "clearing": "razvedrava",
        "thin_veil": "tanak",
    }
    for label, (f, _state) in CASES.items():
        res = verdict.interpret(f)
        sr = verdict.serbian_line(f, res)
        assert sr["text"], f"[{label}] empty serbian line"
        assert want_sub[label] in sr["text"], (
            f"[{label}] serbian line {sr['text']!r} lacks {want_sub[label]!r}")


def test_cloud_verdict_from_status():
    status = {"location": {"name": "Budva"},
              "source": {"ok": True},
              "facts": CASES["overcast"][0],
              "summary": {}}
    v = verdict.cloud_verdict(status)
    assert v and v["state"] == "OVERCAST"
    assert v["headline"] and v["narrative"]
    assert v["line_sr"] and "oblačno" in v["line_sr"]
    # no usable data -> None
    assert verdict.cloud_verdict({"source": {"ok": False}}) is None


def main():
    fails = []
    for fn in (test_states, test_eta_in_narrative, test_serbian_line,
               test_cloud_verdict_from_status):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — cloud verdict state machine OK on all cases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
