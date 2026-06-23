"""Tests for the 3-D growth/decay survival signal (PDF Part C2/B2): the nowcast
survival timescale should prefer the full-volume VIL trend over the 2-D dBZ
trend when VIL is available, since VIL tracks the mixed-phase updraft far better
than peak reflectivity. Falls back to the dBZ trend when there is no volume.

Run from repo root:  python tests/test_survival.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nowcast  # noqa: E402


def test_falling_vil_gives_finite_lifetime_even_if_dbz_flat():
    # VIL collapsing while peak dBZ is flat -> the cell is decaying (3-D signal
    # the 2-D dBZ trend misses) -> finite survival timescale.
    summ = {"vil_trend_per_min": -0.5, "dbz_trend_per_min": 0.0, "trend": "steady"}
    latest = {"max_dbz": 45.0, "vil_kg_m2": 5.0}
    lt = nowcast._lifetime_min(summ, latest)
    assert lt is not None and lt > 0, lt


def test_rising_vil_survives_even_if_dbz_falling():
    # VIL rising (intensifying mixed-phase column) -> survives the window, even
    # though peak dBZ ticked down -> the VIL trend takes precedence.
    summ = {"vil_trend_per_min": 0.5, "dbz_trend_per_min": -2.0, "trend": "decaying"}
    latest = {"max_dbz": 45.0, "vil_kg_m2": 5.0}
    assert nowcast._lifetime_min(summ, latest) is None


def test_falls_back_to_dbz_trend_without_vil():
    # No VIL (PNG path / overshot distant cell) -> unchanged 2-D dBZ behaviour.
    summ = {"dbz_trend_per_min": -2.0, "trend": "decaying"}
    latest = {"max_dbz": 47.0}
    lt = nowcast._lifetime_min(summ, latest)
    assert lt is not None and lt > 0
    # steady dBZ, no VIL -> survives (None)
    assert nowcast._lifetime_min({"dbz_trend_per_min": 0.0}, {"max_dbz": 40.0}) is None


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
    print("\nPASS — 3-D VIL-trend survival OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
