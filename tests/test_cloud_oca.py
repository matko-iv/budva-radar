"""Tests for clouds/oca.py — correct OCA optical-thickness unpacking.

OCA stores COT as log10 in TWO layers; the total must be summed in
LINEAR space (10^upper + 10^lower), fill must be masked BEFORE de-logging, and
failed retrievals (scene_classification == 10) dropped. COT ~= 257 (10^2.41) is
SATURATION of a thick cloud, not a fill value or a bug.

Run from repo root:  python tests/test_cloud_oca.py   (exit 0 = pass)
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from clouds import oca  # noqa: E402

NAN = float("nan")


def test_delog_basic():
    out = oca.delog(np.array([0.0, 1.0, 2.0]))
    assert np.allclose(out, [1.0, 10.0, 100.0]), out


def test_delog_preserves_nan():
    out = oca.delog(np.array([1.0, NAN]))
    assert out[0] == 10.0 and np.isnan(out[1]), out


def test_total_cot_sums_two_layers_in_linear_space():
    # 10^1 + 10^log10(90) = 10 + 90 = 100. Summing in LOG space would be wrong.
    upper = np.array([1.0])
    lower = np.array([np.log10(90.0)])
    out = oca.total_cot(upper, lower)
    assert np.allclose(out, [100.0]), out


def test_total_cot_upper_only_when_lower_missing():
    upper = np.array([1.0, 2.0])
    lower = np.array([NAN, NAN])
    out = oca.total_cot(upper, lower)
    assert np.allclose(out, [10.0, 100.0]), out


def test_total_cot_none_lower_layer():
    out = oca.total_cot(np.array([1.0]), None)
    assert np.allclose(out, [10.0]), out


def test_total_cot_both_nan_is_nan():
    out = oca.total_cot(np.array([NAN]), np.array([NAN]))
    assert np.isnan(out[0]), out


def test_saturation_is_kept_not_dropped():
    # log10 upper limit ~2.41 -> COT ~257: a saturated thick cloud, NOT fill.
    out = oca.total_cot(np.array([2.41]), np.array([NAN]))
    assert 250.0 < out[0] < 260.0, out


def test_scene_filter_drops_failed_retrieval():
    cot = np.array([12.0, 8.0, 30.0])
    scene = np.array([1.0, 10.0, 3.0])   # 10 == failed retrieval
    out = oca.apply_scene_filter(cot, scene)
    assert out[0] == 12.0 and np.isnan(out[1]) and out[2] == 30.0, out


def test_scene_filter_tolerates_none():
    cot = np.array([12.0, 8.0])
    out = oca.apply_scene_filter(cot, None)
    assert np.allclose(out, cot), out


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
    print("\nPASS — OCA COT unpacking OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
