"""Run the SKALA pipelines, each waiting for its own upstream source to advance.

Rather than a fixed cycle, loop.py polls each module's source on a short tick
and runs the pipeline only when a newer frame has appeared:
  * SKALA RAIN  (run.py)        — newest OPERA composite epoch (~5-min cadence)
  * SKALA CLOUD (run_clouds.py) — freshest HighSight slot (10-min cadence,
                                  lagged), gated to the tile-quota interval
  * NOWCAST (compare_nowcast.py --ord-latest) — newest ORD (hrulj) volume;
                                  opt-in, meant for its own terminal via
                                  --only-nowcast since the DGMR comparison is
                                  slower than the RAIN/CLOUD cadence
On a successful run docs/ is committed and pushed immediately.

Flags:
  --no-push        skip the git commit/push (serve docs/ locally instead)
  --no-rain        don't watch / run the radar pipeline
  --no-cloud       don't watch / run the satellite pipeline
  --nowcast        also watch ORD volumes + run compare_nowcast.py
  --only-nowcast   watch only the nowcast comparison (implies --no-rain --no-cloud)
  --once           run any module whose source is already new once, then exit
  --poll SEC       source-check interval in seconds (default 60)

The cloud pipeline needs HIGHSIGHT_KEY (or EUMETSAT creds for the L2 path); an
unreachable source just makes that module wait while the others keep running.
"""

import time
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from radar import r2_publish

BASE_DIR = Path(__file__).resolve().parent
# With R2 configured the modules already publish there, so pushing to GitHub
# (slow Pages rebuild) is only the fallback when R2 isn't set up, or forced
# with --push. --no-push always disables it.
PUSH_TO_GIT = ("--no-push" not in sys.argv
               and ("--push" in sys.argv or not r2_publish.available()))
ONLY_NOWCAST = "--only-nowcast" in sys.argv
RUN_RAIN = "--no-rain" not in sys.argv and not ONLY_NOWCAST
RUN_CLOUD = "--no-cloud" not in sys.argv and not ONLY_NOWCAST
RUN_NOWCAST = "--nowcast" in sys.argv or ONLY_NOWCAST

DEFAULT_POLL_SEC = 60                  # how often to check the upstream sources
RAIN_MIN_GAP_SEC = 0                   # OPERA's 5-min epoch already gates re-runs
# Don't re-run CLOUD faster than the HighSight tile-quota throttle (else we burn
# the free tile quota); newer 10-min slots inside this gap are skipped.
CLOUD_MIN_GAP_SEC = int(config.CLOUDS.get("highsight_min_interval_min", 20)) * 60
# NOWCAST's token is the newest ORD volume time, which only advances when a new
# volume lands (~radar cadence), so no extra floor is needed.
NOWCAST_MIN_GAP_SEC = 0


def push_docs():
    """Commit + push docs/. Silent on errors - if git fails (no network,
    not a repo, etc.) just skip and continue the loop.

    Race-condition handling: if GitHub Actions pushed between our pull and
    our push, we retry pull --rebase + push up to 3 times before giving up.
    """
    try:
        # Stage everything the GH Actions workflow stages, so locally produced
        # frames + status files don't pile up as untracked changes.
        subprocess.run(["git", "add", "docs/"], cwd=BASE_DIR,
                       check=False, capture_output=True, timeout=15)
        subprocess.run(["git", "add", "output/status.json", "output/cloud_status.json"],
                       cwd=BASE_DIR, check=False, capture_output=True, timeout=15)
        # Use -A so frame rotations (additions AND deletions) are both tracked.
        # highsight_frames is included so the tile-quota throttle + nowcast motion
        # survive a restart/redeploy (the throttle relies on the cache persisting).
        subprocess.run(["git", "add", "-A", "data/frames/", "data/cloud_frames/",
                        "data/highsight_frames/"],
                       cwd=BASE_DIR, check=False, capture_output=True, timeout=15)
        msg = f"skala update {datetime.now():%Y-%m-%d %H:%M:%S}"
        subprocess.run([
            "git",
            "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", msg,
        ], cwd=BASE_DIR, check=False, capture_output=True, timeout=15)

        for attempt in range(1, 4):
            pull = subprocess.run(
                ["git", "pull", "--rebase", "--autostash"], cwd=BASE_DIR,
                check=False, capture_output=True, timeout=30,
            )
            if pull.returncode != 0:
                err = (pull.stderr or b"").decode(errors="replace").strip().splitlines()
                tail = err[-1] if err else "(no stderr)"
                # Abort an in-progress rebase if one was left behind
                subprocess.run(["git", "rebase", "--abort"], cwd=BASE_DIR,
                               check=False, capture_output=True, timeout=10)
                print(f"  git: pull failed (attempt {attempt}) - {tail}")
                continue

            push = subprocess.run(["git", "push"], cwd=BASE_DIR,
                                   check=False, capture_output=True, timeout=30)
            if push.returncode == 0:
                print(f"  git: pushed (attempt {attempt})")
                return
            err = (push.stderr or b"").decode(errors="replace").strip().splitlines()
            tail = err[-1] if err else "(no stderr)"
            print(f"  git: push attempt {attempt} failed - {tail}")

        print("  git: gave up after 3 attempts (likely GH Actions race)")
    except Exception as e:
        print(f"  git: skip ({e})")


def _run(cmd):
    """Run a pipeline command (script + optional args) with our interpreter; never
    let a crash kill the loop. `cmd` is a list, e.g. ["run.py"] or
    ["compare_nowcast.py", "--ord-latest"]."""
    try:
        subprocess.run([sys.executable, *cmd], check=False, cwd=BASE_DIR)
    except Exception as e:
        print(f"{' '.join(cmd)} crashed: {e}")


def _rain_source_token():
    """RAIN's freshness signal: the newest OPERA composite epoch (ms) upstream.
    A lightweight JSON GET — returns None if the listing can't be reached (the
    module then just waits and retries next tick)."""
    import requests
    src = config.SOURCES["opera"]
    r = requests.get(src["list_url"], timeout=20,
                     headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    epochs = [it.get("epoch") for it in r.json().get("images", []) if it.get("epoch")]
    return max(epochs) if epochs else None


def _cloud_source_token():
    """CLOUD's freshness signal: the freshest HighSight slot (UTC, floored to the
    10-min cadence, behind the publish lag) as an ISO string. Mirrors
    clouds.highsight._freshest_slot without importing the heavy module."""
    cfg = config.CLOUDS
    lag = int(cfg.get("highsight_lag_min", 30)) if cfg.get("use_highsight", False) else 0
    t = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=lag)
    return t.replace(minute=(t.minute // 10) * 10, second=0, microsecond=0).isoformat()


def _nowcast_source_token():
    """NOWCAST's freshness signal: the newest ORD (hrulj) volume's nominal time (UTC
    ISO). Two lightweight S3 listings via radar.ord; returns None if the bucket can't
    be reached (the module then just waits and retries next tick)."""
    from radar import ord as o
    times = [t1 for (_d, n, _t0, t1) in o.available_window() if n and t1]
    return max(times).isoformat() if times else None


class Watcher:
    """Watches one module's upstream source and runs its pipeline only when the
    source token advances (and at least min_gap_sec has passed since the last run)."""

    def __init__(self, name, script, token_fn, min_gap_sec):
        self.name = name
        # `script` may be a bare script name or a [script, *args] list.
        self.cmd = [script] if isinstance(script, str) else list(script)
        self.token_fn = token_fn
        self.min_gap_sec = min_gap_sec
        self.last_token = None
        self.last_run = 0.0

    def check_and_run(self):
        """Run the pipeline if the source advanced. Returns True iff it ran (so the
        caller knows to push docs/)."""
        try:
            token = self.token_fn()
        except Exception as e:
            print(f"  [{self.name}] source check failed: {e}")
            return False
        if token is None or token == self.last_token:
            return False
        if (time.time() - self.last_run) < self.min_gap_sec:
            return False                       # newer source, but inside the min gap
        print(f"  [{self.name}] new source ({token}) -> running {' '.join(self.cmd)}")
        _run(self.cmd)
        self.last_token = token
        self.last_run = time.time()
        return True


def _poll_seconds():
    if "--poll" in sys.argv:
        try:
            return max(10, int(sys.argv[sys.argv.index("--poll") + 1]))
        except (IndexError, ValueError):
            print("  bad --poll value; using default")
    return DEFAULT_POLL_SEC


def main():
    once = "--once" in sys.argv
    poll = _poll_seconds()
    watchers = []
    if RUN_RAIN:
        watchers.append(Watcher("RAIN", "run.py", _rain_source_token, RAIN_MIN_GAP_SEC))
    if RUN_CLOUD:
        watchers.append(Watcher("CLOUD", "run_clouds.py", _cloud_source_token, CLOUD_MIN_GAP_SEC))
    if RUN_NOWCAST:
        # compare_nowcast.py mirrors its outputs to R2 itself (instant); --no-push lets
        # loop.py remain the single git-push authority (its push_docs runs below only
        # when PUSH_TO_GIT), exactly like RAIN/CLOUD.
        watchers.append(Watcher("NOWCAST", ["compare_nowcast.py", "--ord-latest", "--no-push"],
                                _nowcast_source_token, NOWCAST_MIN_GAP_SEC))

    mode = ("git push (R2 not configured)" if PUSH_TO_GIT
            else ("R2 publish, no git push" if r2_publish.available() else "no push"))
    names = " + ".join(w.name for w in watchers) or "nothing"
    print(f"SKALA loop [{names}] — each module waits for its OWN source to advance "
          f"(poll {poll}s), {mode}. Ctrl+C to stop.")
    while True:
        t0 = datetime.now()
        print(f"\n=== {t0.isoformat(timespec='seconds')} (checking sources) ===")
        for w in watchers:
            if w.check_and_run() and PUSH_TO_GIT:
                push_docs()
        if once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
