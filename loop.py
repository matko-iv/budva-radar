"""Run the SKALA pipelines every N minutes until killed with Ctrl+C.

Each cycle runs BOTH modules — run.py (SKALA RAIN, radar) and run_clouds.py
(SKALA CLOUD, satellite) — then commits + pushes docs/ once so GitHub Pages
picks up both radar_status.json and cloud_status.json.

Flags:
  --no-push    skip the git commit/push (serve docs/ locally instead)
  --no-rain    skip the radar pipeline this run
  --no-cloud   skip the satellite pipeline (e.g. no EUMETSAT credentials)

The cloud pipeline needs EUMETSAT_KEY / EUMETSAT_SECRET in the environment; if
they're missing it just logs and the loop carries on with radar only.
"""

import time
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config

INTERVAL_SEC = config.FETCH_INTERVAL_MIN * 60
BASE_DIR = Path(__file__).resolve().parent
PUSH_TO_GIT = "--no-push" not in sys.argv
RUN_RAIN = "--no-rain" not in sys.argv
RUN_CLOUD = "--no-cloud" not in sys.argv


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


def _run(script):
    """Run a pipeline script; never let a crash kill the loop."""
    try:
        subprocess.run([sys.executable, script], check=False, cwd=BASE_DIR)
    except Exception as e:
        print(f"{script} crashed: {e}")


def main():
    mode = "with git push" if PUSH_TO_GIT else "no push (--no-push)"
    modules = " + ".join(([("RAIN") ] if RUN_RAIN else []) + (["CLOUD"] if RUN_CLOUD else []))
    print(f"SKALA loop [{modules or 'nothing'}] - interval "
          f"{config.FETCH_INTERVAL_MIN} min, {mode}. Ctrl+C to stop.")
    while True:
        t0 = datetime.now()
        print(f"\n=== {t0.isoformat(timespec='seconds')} ===")
        if RUN_RAIN:
            print("--- SKALA RAIN (run.py) ---")
            _run("run.py")
        if RUN_CLOUD:
            print("--- SKALA CLOUD (run_clouds.py) ---")
            _run("run_clouds.py")
        if PUSH_TO_GIT:
            push_docs()
        elapsed = (datetime.now() - t0).total_seconds()
        sleep_for = max(10, INTERVAL_SEC - elapsed)
        print(f"sleeping {sleep_for:.0f}s ...")
        time.sleep(sleep_for)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBye.")
