"""One-off: replay skala_log with stricter 'approaching' gates to find what cuts
FAR (currently 0.721, POD 1.0). Mirrors verification.score_log semantics exactly:
matured DRY-state scans, observed = rain_at_location within 60 min lookahead.
csv/stdlib only. Run: py -3.13 _far_sweep.py
"""
import csv
import datetime
from pathlib import Path

HORIZON_MIN = 60
DOCS = Path(__file__).parent / "docs"


def parse_dt(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", ""))
    except Exception:
        return None


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


rows = []
for p in sorted(DOCS.glob("skala_log_*.csv")):
    with open(p, newline="", encoding="utf-8") as f:
        rows.extend(list(csv.DictReader(f)))
rows = [r for r in rows if parse_dt(r.get("generated"))]
rows.sort(key=lambda r: parse_dt(r["generated"]))
times = [parse_dt(r["generated"]) for r in rows]
at_loc = [r.get("rain_at_location") == "1" for r in rows]
last_t = times[-1]

# Precompute matured dry-scan indices + observed outcome (same as score_log).
scored = []  # (i, observed)
for i, r in enumerate(rows):
    if (last_t - times[i]).total_seconds() < HORIZON_MIN * 60:
        continue
    if at_loc[i]:
        continue
    observed = False
    j = i + 1
    while j < len(rows) and (times[j] - times[i]).total_seconds() <= HORIZON_MIN * 60:
        if at_loc[j]:
            observed = True
            break
        j += 1
    scored.append((i, observed))


def contingency(pred_fn):
    H = M = F = C = 0
    for i, obs in scored:
        p = pred_fn(i)
        if p and obs: H += 1
        elif p and not obs: F += 1
        elif (not p) and obs: M += 1
        else: C += 1
    pod = H / (H + M) if (H + M) else None
    far = F / (H + F) if (H + F) else None
    csi = H / (H + M + F) if (H + M + F) else None
    n = H + M + F + C
    exp = ((H + M) * (H + F) + (C + M) * (C + F)) / n if n else 0
    hss = (H + C - exp) / (n - exp) if n and (n - exp) else None
    return H, M, F, C, pod, far, csi, hss


def show(name, pred_fn):
    H, M, F, C, pod, far, csi, hss = contingency(pred_fn)
    fmt = lambda v: "  -  " if v is None else f"{v:.3f}"
    print(f"{name:<46} H={H:<4} M={M:<3} F={F:<4} C={C:<4} "
          f"POD={fmt(pod)} FAR={fmt(far)} CSI={fmt(csi)} HSS={fmt(hss)}")


def base(i):
    return rows[i].get("rain_approaching") == "1"


print(f"matured dry scans: {len(scored)}, onsets observed: {sum(1 for _, o in scored if o)}\n")

show("BASELINE: rain_approaching", base)

print("\n-- by scenario state --")
show("state >= LIKELY (drop POSSIBLE)",
     lambda i: base(i) and rows[i]["scenario_state"] in ("IMMINENT", "LIKELY", "SEVERE"))
show("state == IMMINENT/SEVERE",
     lambda i: base(i) and rows[i]["scenario_state"] in ("IMMINENT", "SEVERE"))

print("\n-- by dominant-cell ETA --")
for eta_max in (90, 60, 45, 30):
    show(f"approaching & eta <= {eta_max} min",
         lambda i, e=eta_max: base(i) and (fnum(rows[i].get("closest_eta_minutes")) is not None)
         and fnum(rows[i]["closest_eta_minutes"]) <= e)

print("\n-- by dominant-cell distance --")
for km_max in (150, 100, 75, 50, 30):
    show(f"approaching & dist <= {km_max} km",
         lambda i, k=km_max: base(i) and (fnum(rows[i].get("closest_rain_km")) is not None)
         and fnum(rows[i]["closest_rain_km"]) <= k)

print("\n-- by dominant-cell intensity --")
for dbz_min in (25, 30, 35):
    show(f"approaching & dbz >= {dbz_min}",
         lambda i, d=dbz_min: base(i) and (fnum(rows[i].get("closest_rain_dbz")) is not None)
         and fnum(rows[i]["closest_rain_dbz"]) >= d)

print("\n-- persistence (consecutive approaching scans) --")
def persist(i, n):
    # current + previous n-1 rows all approaching (rows are ~7 min apart)
    for k in range(n):
        j = i - k
        if j < 0 or rows[j].get("rain_approaching") != "1":
            return False
    return True
for n in (2, 3):
    show(f"approaching in {n} consecutive scans", lambda i, n=n: persist(i, n))

print("\n-- combinations --")
show("persist 2 & eta <= 60",
     lambda i: persist(i, 2) and (fnum(rows[i].get("closest_eta_minutes")) is not None)
     and fnum(rows[i]["closest_eta_minutes"]) <= 60)
show("persist 2 & dist <= 100",
     lambda i: persist(i, 2) and (fnum(rows[i].get("closest_rain_km")) is not None)
     and fnum(rows[i]["closest_rain_km"]) <= 100)
show("persist 2 & state >= LIKELY",
     lambda i: persist(i, 2) and rows[i]["scenario_state"] in ("IMMINENT", "LIKELY", "SEVERE"))
show("eta <= 60 & dist <= 100",
     lambda i: base(i) and (fnum(rows[i].get("closest_eta_minutes")) is not None)
     and fnum(rows[i]["closest_eta_minutes"]) <= 60
     and (fnum(rows[i].get("closest_rain_km")) is not None)
     and fnum(rows[i]["closest_rain_km"]) <= 100)

print("\n-- false-alarm anatomy (baseline) --")
fa_state, hit_state = {}, {}
fa_km, hit_km = [], []
fa_eta = []
for i, obs in scored:
    if not base(i):
        continue
    st = rows[i]["scenario_state"]
    km = fnum(rows[i].get("closest_rain_km"))
    eta = fnum(rows[i].get("closest_eta_minutes"))
    if obs:
        hit_state[st] = hit_state.get(st, 0) + 1
        if km is not None: hit_km.append(km)
    else:
        fa_state[st] = fa_state.get(st, 0) + 1
        if km is not None: fa_km.append(km)
        if eta is not None: fa_eta.append(eta)
print("hits by state:        ", dict(sorted(hit_state.items())))
print("false alarms by state:", dict(sorted(fa_state.items())))
if hit_km:
    hit_km.sort(); fa_km.sort()
    med = lambda a: a[len(a)//2]
    print(f"median dominant-cell dist: hits {med(hit_km):.0f} km vs FA {med(fa_km):.0f} km")
if fa_eta:
    fa_eta.sort()
    print(f"FA with an ETA: {len(fa_eta)}; median FA eta {fa_eta[len(fa_eta)//2]:.0f} min")
