"""Run run.py every N minutes until killed with Ctrl+C.

By default also commits + pushes docs/ to the git remote after each run,
so GitHub Pages picks up the new radar_status.json. Pass --no-push to skip
the git step (useful when serving docs/ directly over HTTP locally).
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


def push_docs():
    """Commit + push docs/. Silent on errors - if git fails (no network,
    not a repo, etc.) just skip and continue the loop."""
    try:
        subprocess.run(["git", "add", "docs/"], cwd=BASE_DIR,
                       check=False, capture_output=True, timeout=15)
        msg = f"radar update {datetime.now():%Y-%m-%d %H:%M}"
        subprocess.run(["git", "commit", "-m", msg],
                       cwd=BASE_DIR, check=False, capture_output=True, timeout=15)
        r = subprocess.run(["git", "push"], cwd=BASE_DIR,
                           check=False, capture_output=True, timeout=30)
        if r.returncode == 0:
            print("  git: pushed")
        else:
            err = (r.stderr or b"").decode(errors="replace").strip().splitlines()
            tail = err[-1] if err else "(no stderr)"
            print(f"  git: push failed - {tail}")
    except Exception as e:
        print(f"  git: skip ({e})")


def main():
    mode = "with git push" if PUSH_TO_GIT else "no push (--no-push)"
    print(f"budva-radar loop - interval {config.FETCH_INTERVAL_MIN} min, {mode}. "
          f"Ctrl+C to stop.")
    while True:
        t0 = datetime.now()
        print(f"\n=== {t0.isoformat(timespec='seconds')} ===")
        try:
            subprocess.run([sys.executable, "run.py"], check=False, cwd=BASE_DIR)
        except Exception as e:
            print(f"run.py crashed: {e}")
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
