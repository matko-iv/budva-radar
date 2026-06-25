"""Download MeteoGate ORD (hrulj / Uljenje) ODIM volumes for a chosen time window.

The upstream bucket (openradar-24h) keeps only a ROLLING ~24 h, so you can fetch any
range within roughly the last 24-38 h; older ranges come back empty. Run --list to
see the exact boundaries available right now.

    python fetch_ord.py --list                                  # what's available now
    python fetch_ord.py --last 3h                               # now-3h .. now
    python fetch_ord.py --from "2026-06-24 13:00" --to "2026-06-24 14:00"   # UTC range
    python fetch_ord.py --from "2026-06-24 15:00" --to "2026-06-24 17:00" --local
    python fetch_ord.py --from ... --to ... --dest data/frames/ord          # custom dir

Times are UTC by default; with --local they are your machine's local zone (and the
radar times are UTC, so a 13:45 UTC frame is 15:45 in CEST). Files land in
data/ord_archive/ by default — the live loop never prunes that folder. Feed a window
straight into the comparison page:

    python compare_nowcast.py --h5 data/ord_archive
"""

import datetime
import re
import sys

from radar import ord as o


def _local_tz():
    return datetime.datetime.now().astimezone().tzinfo


def _parse_dt(s, local):
    """'YYYY-MM-DD HH:MM[:SS]' -> tz-aware UTC datetime (interpreted local if asked)."""
    s = s.strip()
    t = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            t = datetime.datetime.strptime(s, fmt)
            break
        except ValueError:
            t = None
    if t is None:
        raise SystemExit(f"bad time '{s}'; use 'YYYY-MM-DD HH:MM' (UTC, or add --local)")
    tz = _local_tz() if local else datetime.timezone.utc
    return t.replace(tzinfo=tz).astimezone(datetime.timezone.utc)


def _parse_dur(s):
    """'3h', '90m', '2h30m' -> timedelta."""
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*", s or "")
    if not m or not (m.group(1) or m.group(2)):
        raise SystemExit(f"bad duration '{s}'; use e.g. 3h, 90m, 2h30m")
    return datetime.timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def _fmt(t, local):
    if t is None:
        return "—"
    z = t.astimezone(_local_tz()) if local else t
    return z.strftime("%Y-%m-%d %H:%M") + (" local" if local else " UTC")


def _parse_argv(argv):
    opts = {"list": False, "local": False, "from": None, "to": None,
            "last": None, "dest": None}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--list":
            opts["list"] = True
        elif a == "--local":
            opts["local"] = True
        elif a in ("--from", "--to", "--last", "--dest") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 1
        else:
            raise SystemExit(f"unknown/incomplete arg: {a}\n\n{__doc__}")
        i += 1
    return opts


def main(argv):
    opts = _parse_argv(argv)

    if opts["list"] or len(argv) == 1:
        print("MeteoGate ORD (hrulj) — currently fetchable window (rolling ~24 h):")
        for d, n, t0, t1 in o.available_window():
            span = f"{_fmt(t0, opts['local'])}  ->  {_fmt(t1, opts['local'])}" if n else "(empty)"
            print(f"  {d}: {n:4d} volumes   {span}")
        if not opts["list"]:
            print("\nGive a window, e.g.:  python fetch_ord.py --last 3h")
        return 0

    if opts["last"]:
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - _parse_dur(opts["last"])
    elif opts["from"] and opts["to"]:
        start = _parse_dt(opts["from"], opts["local"])
        end = _parse_dt(opts["to"], opts["local"])
    else:
        raise SystemExit("give either --last <dur> or both --from and --to "
                         "(see --list for what's available)")

    dest = opts["dest"] or str(o.ORD_ARCHIVE_DIR)
    print(f"Fetching ORD volumes {_fmt(start, opts['local'])}  ->  {_fmt(end, opts['local'])}")
    if opts["local"]:
        print(f"  (UTC: {start.strftime('%Y-%m-%d %H:%M')} -> {end.strftime('%Y-%m-%d %H:%M')})")
    paths = o.fetch_range(start, end, dest=dest)
    print(f"Downloaded {len(paths)} volume(s) into {dest}")
    if paths:
        print(f"  first: {paths[0].name}")
        print(f"  last:  {paths[-1].name}")
        print(f"\nUse them:  python compare_nowcast.py --h5 {dest}")
    else:
        print("  Nothing in that range. It may be outside the rolling ~24 h window — "
              "run  python fetch_ord.py --list  to see what's available.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
