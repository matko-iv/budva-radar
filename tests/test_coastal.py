"""Tests for radar/coastal.py — the coastal-arrival score:

Whether an inland cell actually reaches the Adriatic coast depends on (a) the
steering flow toward the coast, (b) the VIL/echo-top growth-decay trend, and
(c) the Dinaric coastal ridge as a 'dissipation filter' (cells that must descend
the seaward slope tend to rain out / evaporate). Lightning (Part D) is omitted.

Run from repo root:  python tests/test_coastal.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar import coastal  # noqa: E402

# Budva: open sea is to the SW (~225 deg), the Dinaric/Rumija ridge inland to NE.
SEAWARD = 225.0


def test_inland_cell_moving_seaward_descends_ridge():
    # Cell to the NE of Budva (bearing 45) moving SW (225) -> crosses the ridge
    # descending toward the coast.
    assert coastal.descends_seaward(45.0, 225.0, SEAWARD) is True


def test_offshore_cell_moving_onshore_does_not_descend():
    # Cell already SW of Budva (over the sea) moving NE onshore -> no ridge descent.
    assert coastal.descends_seaward(225.0, 45.0, SEAWARD) is False


def test_inland_cell_moving_along_ridge_does_not_descend():
    # Inland cell moving NW (315, along the ridge, not toward the coast).
    assert coastal.descends_seaward(45.0, 315.0, SEAWARD) is False


def test_score_growing_onshore_is_high():
    s = coastal.coastal_arrival_score(0.8, vil_trend_per_min=0.5, descends_ridge=False)
    assert s["score"] > 0.8, s
    assert s["label"] == "likely"


def test_score_decaying_descending_is_suppressed():
    # Strong base, but decaying AND descending the seaward ridge -> down-weighted.
    s = coastal.coastal_arrival_score(0.8, vil_trend_per_min=-0.5, descends_ridge=True)
    assert s["score"] < 0.4, s
    assert s["label"] in ("possible", "unlikely")


def test_score_uses_dbz_trend_when_no_vil():
    s_grow = coastal.coastal_arrival_score(0.6, dbz_trend_per_min=2.0, descends_ridge=False)
    s_decay = coastal.coastal_arrival_score(0.6, dbz_trend_per_min=-2.0, descends_ridge=False)
    assert s_grow["score"] > s_decay["score"]


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
        except Exception as e:
            fails.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    if fails:
        print(f"\n{len(fails)} failure(s).")
        return 1
    print("\nPASS — coastal-arrival score OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
