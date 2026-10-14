"""Daily update: fetch new games, verify, then publish the page.

    python update.py              # the job CI runs
    python update.py --dry-run    # compute and verify, write nothing
    python update.py --skip-verify

Order matters. The page is written only after verify.py exits clean, so a bad
run leaves the previously published page untouched and exits non-zero for CI to
report. Nothing is written between the pipeline and the gate.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from nhlapm import config, output, site
from nhlapm.fetch import Fetcher
from nhlapm.pipeline import run_team
from nhlapm.site import SiteData

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default=config.SEASON)
    parser.add_argument("--teams", default=",".join(config.TEAMS),
                        help=f"comma-separated team codes (default {','.join(config.TEAMS)})")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--docs-dir", default=str(ROOT / config.DOCS_DIR))
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything but write no files")
    parser.add_argument("--skip-verify", action="store_true",
                        help="bypass the publish gate (local use only)")
    parser.add_argument("--retry-empty-shifts", action="store_true")
    return parser.parse_args(argv)


def run_verify(team: str, season: str, cache_dir: str) -> tuple[bool, str]:
    """Run verify.py as its own process; its exit code is the gate.

    Shelling out keeps verify.py the single definition of what 'verified'
    means, rather than a second copy of the checks living in here.
    """
    completed = subprocess.run(
        [sys.executable, str(ROOT / "verify.py"),
         "--team", team, "--season", season, "--cache-dir", cache_dir],
        capture_output=True, text=True, cwd=str(ROOT))
    return completed.returncode == 0, (completed.stdout or "") + (completed.stderr or "")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    teams = [t.strip().upper() for t in args.teams.split(",") if t.strip()]
    docs_dir = Path(args.docs_dir)
    started = datetime.now(timezone.utc)

    print(f"nhl-apm update | season {args.season} | teams: {', '.join(teams)}")
    fetcher = Fetcher(Path(args.cache_dir), verbose=True)

    # -- 1. pipeline ------------------------------------------------------
    runs = []
    for team in teams:
        run = run_team(fetcher, team, args.season,
                       retry_empty_shifts=args.retry_empty_shifts,
                       on_note=print)
        if run.error:
            print(f"FAIL [{team}]: {run.error}", file=sys.stderr)
            return 1
        included = len(run.stats.games)
        print(f"  {team}: {included} game(s) included, {len(run.skipped)} skipped, "
              f"{len(run.newly_fetched)} newly fetched, {len(run.rows)} skaters")
        for label, reason in run.skipped:
            print(f"    skipped {label}: {reason}")
        runs.append(run)

    print(f"  network: {fetcher.api_calls} request(s), {fetcher.cache_hits} cache hit(s)")

    if not any(run.rows for run in runs):
        print("No usable data for any team; refusing to publish an empty page.",
              file=sys.stderr)
        return 1

    # -- 2. publish gate --------------------------------------------------
    if args.skip_verify:
        print("\nVERIFY SKIPPED (--skip-verify)")
    else:
        print("\nVerifying before publish...")
        failures = []
        for team in teams:
            ok, log = run_verify(team, args.season, args.cache_dir)
            tail = [line for line in log.strip().splitlines() if line.strip()][-6:]
            print(f"  [{team}] {'PASS' if ok else 'FAIL'}")
            for line in tail:
                print(f"    {line}")
            if not ok:
                failures.append(team)
        if failures:
            print(f"\nPUBLISH BLOCKED: verification failed for {', '.join(failures)}.",
                  file=sys.stderr)
            print("The previously published page is unchanged.", file=sys.stderr)
            return 1
        print("  all checks passed")

    # -- 3. publish -------------------------------------------------------
    page = site.render_page(SiteData(runs=runs, generated_at_utc=started))

    if args.dry_run:
        print(f"\nDRY RUN: page would be {len(page):,} bytes; nothing written.")
        return 0

    docs_dir.mkdir(parents=True, exist_ok=True)
    index = docs_dir / "index.html"
    index.write_text(page, encoding="utf-8")
    # Keeps GitHub Pages from running the output through Jekyll.
    (docs_dir / ".nojekyll").write_text("", encoding="utf-8")

    written = [index]
    for run in runs:
        if not run.rows:
            continue
        name = (config.PRIMARY_CSV_NAME if run.team == "CHI"
                else f"{run.team.lower()}_adjusted_pm.csv")
        csv_path = output.write_csv(run.rows, ROOT / name)
        shutil.copyfile(csv_path, docs_dir / name)
        written.extend([csv_path, docs_dir / name])

    print(f"\nPublished {index} ({len(page):,} bytes)")
    for path in written[1:]:
        print(f"  also wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
