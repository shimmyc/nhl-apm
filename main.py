"""Adjusted plus-minus at 5v5 with both goalies in net, from the terminal.

    python main.py                      # Blackhawks, 2026-27, computed from the feeds
    python main.py --team BOS
    python main.py --league             # league-wide top/bottom from the published data
    python main.py --league --min-toi 50 --top 10

Club mode fetches (or reads from cache) that club's games and runs exactly the
per-game code the daily job runs. League mode reads docs/data/, i.e. the file
the website serves, and folds it with the same aggregation the page uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nhlapm import aggregate, config, output, parse, pipeline, store
from nhlapm.fetch import Fetcher

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", default=config.DEFAULT_PAGE_TEAM,
                        help="team abbreviation (default CHI)")
    parser.add_argument("--season", default=config.SEASON, help="season id (default 20262027)")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--docs-dir", default=str(ROOT / config.DOCS_DIR))
    parser.add_argument("--out", default=None, help="CSV path for club mode")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore the cache and refetch everything")
    parser.add_argument("--min-toi", type=float, default=0.0,
                        help="hide skaters below this many 5v5 minutes from the table")
    parser.add_argument("--no-html-fallback", action="store_true",
                        help="do not parse the HTML TOI reports when shiftcharts is empty")
    parser.add_argument("--league", action="store_true",
                        help="rank the whole league from the published season file")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--quiet-fetch", action="store_true")
    return parser.parse_args(argv)


def csv_name_for(team: str) -> str:
    return (config.PRIMARY_CSV_NAME if team.upper() == "CHI"
            else f"{team.lower()}_adjusted_pm.csv")


def league_mode(args) -> int:
    season = store.load_season(Path(args.docs_dir), args.season)
    if season is None:
        print("No published season file; run update.py first.", file=sys.stderr)
        return 1
    players = store.load_players(Path(args.docs_dir))
    rows = aggregate.aggregate(season.rows, players)
    shown = [r for r in rows if r.toi_5v5 >= args.min_toi]
    print(f"League-wide | {len(season.games)} games | {len(rows)} skaters, "
          f"{len(shown)} with >= {args.min_toi:g} min 5v5 | data updated {season.updated}")
    output.print_table(shown[:args.top], f"TOP {args.top} BY ADJUSTED")
    output.print_table(shown[-args.top:][::-1], f"BOTTOM {args.top} BY ADJUSTED")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.league:
        return league_mode(args)

    team = args.team.upper()
    csv_path = Path(args.out) if args.out else ROOT / csv_name_for(team)
    fetcher = Fetcher(Path(args.cache_dir), refresh=args.refresh,
                      verbose=not args.quiet_fetch)
    print(f"Adjusted plus-minus | {team} | season {args.season} | 5v5, both goalies in net")

    games, pending = pipeline.discover_team_games(fetcher, team, args.season)
    print(f"Completed regular-season games: {len(games)}  (not yet played: {pending})")
    if not games:
        return 0

    rows_in = []
    names: dict[int, tuple[str, str]] = {}
    excluded = []
    for ref in games:
        outcome = pipeline.process_game(fetcher, ref, args.season,
                                        html_fallback=not args.no_html_fallback)
        if not outcome.ok:
            excluded.append((ref.label(), outcome.reason))
            continue
        record = outcome.record
        rows_in.extend(r for r in record.rows if r.team == team)
        clean = record.clean_seconds / 60
        side = [r for r in record.rows if r.team == team][0]
        print(f"  {ref.label():<36} {clean:6.2f} min  GF {side.team_gf}  "
              f"GA {side.team_ga}  [{record.source}]  verified")

    if not rows_in:
        print("No game produced usable, verified 5v5 data.")
        return 1

    # Names from the pbp roster: club mode must not depend on the published files.
    for ref in games:
        try:
            for pid, player in parse.parse_roster(fetcher.play_by_play(ref.game_id)).items():
                names[pid] = (player.name, player.position)
        except Exception:
            continue

    rows = aggregate.aggregate(rows_in, fallback_names=names)
    output.write_csv(rows, csv_path)
    output.print_table([r for r in rows if r.toi_5v5 >= args.min_toi],
                       f"{team} SKATERS -- ADJUSTED PLUS-MINUS, 5v5 BOTH GOALIES IN NET")
    print("Expected = sum over games of Team_GD_game x (TOI_game / Team_TOI_game)")
    print("Adjusted = GD_on - Expected")
    print("Rel      = GD_on - GD_off x (TOI_on / TOI_off), off-ice over the games played")
    print("NONCLEAN = OFF_PM - GD_on: the part of the official number earned away from clean 5v5")

    toi = sum(r.toi_5v5 for r in rows_in)
    team_clean = sum({r.game_id: r.team_toi_5v5 for r in rows_in}.values())
    adj = sum(r.adjusted for r in rows)
    print()
    print(f"[{'OK' if toi == 5 * team_clean else 'FAIL'}] summed skater TOI {toi}s "
          f"vs 5 x team clean {team_clean}s")
    print(f"[{'OK' if abs(adj) < 1e-6 else 'FAIL'}] summed Adjusted {adj:+.2e}")
    if excluded:
        print(f"\nQuarantined ({len(excluded)}):")
        for label, reason in excluded:
            print(f"  {label}: {reason}")
    print(f"\nCSV written: {csv_path}")
    print(f"API calls: {fetcher.api_calls}   cache hits: {fetcher.cache_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
