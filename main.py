"""Adjusted plus-minus for a club's skaters at 5v5 with both goalies in net.

    python main.py                      # Blackhawks, 2026-27
    python main.py --team BOS
    python main.py --retry-empty-shifts # re-ask the API for games cached empty

The calculation itself lives in nhlapm.pipeline, which the site builder shares.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nhlapm import config, output
from nhlapm.fetch import Fetcher
from nhlapm.pipeline import run_team

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", default=config.TEAM, help="team abbreviation (default CHI)")
    parser.add_argument("--season", default=config.SEASON, help="season id (default 20262027)")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--out", default=None, help="CSV path (default hawks_adjusted_pm.csv)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache and refetch everything")
    parser.add_argument("--retry-empty-shifts", action="store_true",
                        help="refetch only shift responses that cached with zero rows")
    parser.add_argument("--min-toi", type=float, default=0.0,
                        help="hide skaters below this many 5v5 minutes from the console table")
    parser.add_argument("--spotlight", default="Patrick Kane,Ryan Greene",
                        help="comma-separated names to print separately")
    parser.add_argument("--no-html-fallback", action="store_true",
                        help="do not parse the HTML TOI reports when shiftcharts is empty")
    parser.add_argument("--quiet-fetch", action="store_true")
    return parser.parse_args(argv)


def csv_name_for(team: str) -> str:
    return (config.PRIMARY_CSV_NAME if team.upper() == "CHI"
            else f"{team.lower()}_adjusted_pm.csv")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    team = args.team.upper()
    csv_path = Path(args.out) if args.out else ROOT / csv_name_for(team)

    fetcher = Fetcher(Path(args.cache_dir), refresh=args.refresh,
                      verbose=not args.quiet_fetch)

    print(f"Adjusted plus-minus | {team} | season {args.season} | 5v5, both goalies in net")
    print(f"Cache: {fetcher.cache_dir}")

    run = run_team(fetcher, team, args.season,
                   html_fallback=not args.no_html_fallback,
                   retry_empty_shifts=args.retry_empty_shifts,
                   on_note=print)

    if run.error:
        print(f"ERROR: {run.error}", file=sys.stderr)
        return 1

    total_games = len(run.stats.games) + len(run.skipped)
    print(f"Completed regular-season games: {total_games}  "
          f"(not yet played: {run.pending_games})")
    if run.newly_fetched:
        print(f"Newly fetched this run: {len(run.newly_fetched)} game(s)")
    if not total_games:
        print("Nothing to compute yet.")
        return 0

    if not run.stats.games:
        print()
        print("No game produced usable 5v5 data; nothing to report.")
        output.print_validation(run.stats, [], run.skipped, run.pending_games)
        return 1

    rows = run.rows
    written = output.write_csv(rows, csv_path)

    shown = [row for row in rows if row.toi_5v5 >= args.min_toi]
    output.print_table(
        shown,
        f"{team} SKATERS -- ADJUSTED PLUS-MINUS, 5v5 BOTH GOALIES IN NET "
        f"({len(run.stats.games)} games)",
    )
    print("Adjusted = GD_on - Expected, where Expected = Team_GD_5v5 x (TOI_5v5 / Team_TOI_5v5)")
    print("Rel      = GD_on - GD_off x (TOI_5v5 / TOI_off)")
    print("OFF_PM   = official NHL +/- (all situations) over the same games")
    print("NONCLEAN = OFF_PM - GD_on: the part of the official number earned away from clean 5v5")
    if run.skipped:
        print(f"GP       = games included here ({len(run.stats.games)} with usable shift data), "
              f"not season games played; {len(run.skipped)} game(s) excluded")

    spotlight = [name.strip().lower() for name in args.spotlight.split(",") if name.strip()]
    if spotlight:
        picked = [row for row in rows
                  if any(token in row.name.lower() for token in spotlight)]
        if picked:
            ranks = {row.player_id: index + 1 for index, row in enumerate(rows)}
            output.print_table(picked, "SPOTLIGHT")
            for row in picked:
                print(f"  {row.name}: rank {ranks[row.player_id]} of {len(rows)} by Adjusted")
        missing = [token for token in spotlight
                   if not any(token in row.name.lower() for row in rows)]
        if missing:
            print(f"  not found among {team} skaters with 5v5 data: {', '.join(missing)}")

    output.print_validation(run.stats, rows, run.skipped, run.pending_games)
    print()
    print(f"CSV written: {written}")
    print(f"API calls: {fetcher.api_calls}   cache hits: {fetcher.cache_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
