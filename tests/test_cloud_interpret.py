"""Integration tests for clouds/interpret.cloud_facts + clouds/verdict — the
PDF's core outcomes end to end:

  * thin cirrus is PRESENT (counts as cloud) yet the sun GETS THROUGH -> CLEAR,
    not OVERCAST (the original bug);
  * an opaque thick deck -> sun BLOCKED -> OVERCAST;
  * at night OCA COT is unusable -> no sun claim, IR-only wording.

Run from repo root:  python tests/test_cloud_interpret.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import interpret, verdict  # noqa: E402
from clouds.grid import CloudField  # noqa: E402

BUDVA = (42.2864, 18.8400)
DAY = "2026-06-21T10:45:00"     # solar noon over Budva (sun high)
NIGHT = "2026-06-21T23:30:00"   # well after dark


def make_field(sensing_time, *, presence=1.0, opaque=0.0, cot=2.0,
               cth=9000.0, ctt=235.0, phase=2.0):
    lats = np.arange(42.7, 41.9 - 1e-9, -0.03)
    lons = np.arange(18.4, 19.3 + 1e-9, 0.03)
    H, W = len(lats), len(lons)

    def full(v):
        return np.full((H, W), float(v))

    cloudy = presence > 0.0
    return CloudField(lats, lons, {
        "mask": full(1.0 if cloudy else 0.0),
        "frac": full(presence),
        "opaque": full(opaque),
        "ctt": full(ctt) if cloudy else full(np.nan),
        "cth": full(cth) if cloudy else full(np.nan),
        "cot": full(cot) if cloudy else full(np.nan),
        "phase": full(phase) if cloudy else full(np.nan),
    }, meta={"sensing_time": sensing_time})


def test_thin_cirrus_is_present_but_sun_gets_through():
    f = make_field(DAY, presence=1.0, opaque=0.0, cot=2.0, cth=9000.0, phase=2.0)
    facts = interpret.cloud_facts(f, None, *BUDVA, "Budva")
    # Presence counts the cirrus...
    assert facts["cloudFracNow"] and facts["cloudFracNow"] > 0.8, facts["cloudFracNow"]
    # ...but it does not block the sun.
    assert facts["opaqueFracNow"] == 0.0, facts["opaqueFracNow"]
    assert facts["isNight"] is False
    assert facts["sunState"] == "sunny", facts["sunState"]
    assert facts["thinVeil"] is True
    assert "cirrus" in (facts["cloudTypeLabel"] or ""), facts["cloudTypeLabel"]
    res = verdict.interpret(facts)
    assert res["state"] == "CLEAR", res["state"]
    assert "sun gets through" in res["narrative"], res["narrative"]


def test_opaque_deck_blocks_the_sun_overcast():
    f = make_field(DAY, presence=1.0, opaque=1.0, cot=30.0, cth=1200.0,
                   ctt=275.0, phase=1.0)
    facts = interpret.cloud_facts(f, None, *BUDVA, "Budva")
    assert facts["sunState"] == "blocked", facts["sunState"]
    res = verdict.interpret(facts)
    assert res["state"] == "OVERCAST", res["state"]
    sun = verdict.sun_descriptor(facts)
    assert sun and sun["en"] == "sun blocked", sun


def test_night_makes_no_sun_claim():
    f = make_field(NIGHT, presence=1.0, opaque=1.0, cot=30.0, cth=1200.0,
                   ctt=275.0, phase=1.0)
    facts = interpret.cloud_facts(f, None, *BUDVA, "Budva")
    assert facts["isNight"] is True, facts["szaDeg"]
    assert facts["sunState"] is None, facts["sunState"]
    res = verdict.interpret(facts)
    assert "IR" in res["narrative"], res["narrative"]
    assert "sun" not in res["narrative"].lower(), res["narrative"]
    sun = verdict.sun_descriptor(facts)
    assert sun and sun["state"] == "night", sun


def test_clear_sky_has_no_type_and_is_sunny():
    f = make_field(DAY, presence=0.0)
    facts = interpret.cloud_facts(f, None, *BUDVA, "Budva")
    assert not facts["cloudFracNow"], facts["cloudFracNow"]
    assert facts["cloudTypeLabel"] is None
    assert facts["sunState"] == "sunny"
    assert verdict.interpret(facts)["state"] == "CLEAR"


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = []
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            fails.append(f"{fn.__name__}: {e}")
            print(f"FAIL  {fn.__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — two-axis cloud facts + verdict OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
