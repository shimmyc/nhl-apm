"""Check every game against feeds the pipeline does not compute from.

    python verify.py                    # every completed game, league-wide
    python verify.py --team CHI         # one club's games
    python verify.py --game 2026020098 --goals
    python verify.py --stored           # season checks on the published file only

Per game it compares timeline TOI against the boxscore's published TOI,
rebuilds official +/- from goals and shifts, and checks the per-team TOI and
zero-sum identities (definitions in nhlapm/checks.py, shared with update.py).

Exit status follows the publish rule: non-zero only for a systemic problem --
more than the allowed share of games failing, or a season-level identity
breaking. `--strict` makes any single failing game fatal.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nhlapm import checks, config, store
from nhlapm.fetch import Fetcher
from nhlapm.loader import load_game_context
from nhlapm.pipeline import discover_league_games, discover_team_games, process_game
from nhlapm.timeline import build_timeline

ROOT = Path(__file__).resolve().parent


def print_goals(fetcher: Fetcher, ref, season: str) -> None:
    context = load_game_context(fetcher, ref, season)
    timeline = build_timeline(context)
    if timeline is None:
        return
    for goal in context.goals:
        if goal.is_shootout or not goal.is_clean_5v5_code:
            continue
        scorer = context.roster.get(goal.scorer_id)
        print(f"    P{goal.period} {goal.time_in_period} "
              f"by {scorer.name if scorer else goal.scorer_id} [{goal.situation_code}]")
        for team_id, abbrev in ((context.away_id, context.away_abbrev),
                                (context.home_id, context.home_abbrev)):
            skaters = {p for p, pl in context.roster.items()
                       if pl.team_id == team_id and not pl.is_goalie}
            names = sorted(context.roster[p].name
                           for p in timeline.on_ice_at(goal.absolute_second, skaters))
            print(f"        {abbrev}: {', '.join(names)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", default=None, help="limit to one club's games")
    parser.add_argument("--season", default=config.SEASON)
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--docs-dir", default=str(ROOT / config.DOCS_DIR))
    parser.add_argument("--game", type=int, default=None, help="limit to one game id")
    parser.add_argument("--goals", action="store_true",
                        help="print the on-ice skaters for every qualifying goal")
    parser.add_argument("--no-html-fallback", action="store_true")
    parser.add_argument("--stored", action="store_true",
                        help="only run the season checks on the published season file")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero if any single game fails")
    args = parser.parse_args(argv)

    fetcher = Fetcher(Path(args.cache_dir), verbose=False)

    if args.stored:
        season = store.load_season(Path(args.docs_dir), args.season)
        if season is None:
            print("no published season file")
            return 1
        verdict = checks.verify_season(season, None)
        for line in verdict.lines + [f"BLOCKING: {b}" for b in verdict.blocking]:
            print(line)
        return 0 if verdict.ok else 1

    if args.team:
        games, _ = discover_team_games(fetcher, args.team.upper(), args.season)
    else:
        games, _ = discover_league_games(fetcher, args.season)
    if args.game:
        games = [g for g in games if g.game_id == args.game]

    failed = []
    players_checked = 0
    worst = 0
    for ref in games:
        outcome = process_game(fetcher, ref, args.season,
                               html_fallback=not args.no_html_fallback)
        verdict = outcome.verdict
        if verdict is not None:
            players_checked += verdict.players_checked
            worst = max(worst, verdict.worst_toi_delta)
        source = outcome.record.source if outcome.record else "-"
        status = "OK" if outcome.ok else f"FAIL: {outcome.reason}"
        print(f"{ref.game_id} {ref.date} {ref.away_abbrev}@{ref.home_abbrev}  "
              f"{source:<11} {status}")
        if not outcome.ok:
            failed.append(ref)
        if args.goals:
            print_goals(fetcher, ref, args.season)

    share = len(failed) / len(games) if games else 0.0
    print()
    print(f"Games checked                         : {len(games)}")
    print(f"Games failing (would be quarantined)  : {len(failed)} ({share:.1%})")
    print(f"Player-games compared to boxscore TOI : {players_checked}")
    print(f"Largest TOI difference seen           : {worst}s "
          f"(tolerance {checks.TOLERANCE_SECONDS}s)")
    if args.strict:
        return 0 if not failed else 1
    return 0 if len(failed) <= config.quarantine_limit(len(games)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
