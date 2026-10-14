"""Check the timeline against a feed the pipeline does not otherwise use.

The boxscore publishes the NHL's own per-player time on ice and shift count.
If the second-by-second timeline built from the shift charts is right, total
occupancy per player must reproduce those numbers. Agreement here is evidence
from outside the fetch/parse/timeline path, not a restatement of it.

    python verify.py
    python verify.py --game 2026020098 --goals
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nhlapm import config, parse, stats as stats_mod
from nhlapm.fetch import Fetcher
from nhlapm.loader import load_game_context
from nhlapm.timeline import build_timeline

ROOT = Path(__file__).resolve().parent
TOLERANCE_SECONDS = 2


def boxscore_toi(fetcher: Fetcher, game_id: int) -> dict[int, tuple[int, int]]:
    """playerId -> (toi seconds, shift count) from the NHL boxscore."""
    url = f"{config.BASE_API_WEB}/gamecenter/{game_id}/boxscore"
    payload = fetcher.get(url, f"boxscore_{game_id}")
    out: dict[int, tuple[int, int]] = {}
    for side in ("homeTeam", "awayTeam"):
        for group in ("forwards", "defense", "goalies"):
            for entry in payload.get("playerByGameStats", {}).get(side, {}).get(group, []):
                toi = entry.get("toi")
                if not toi:
                    continue
                out[entry["playerId"]] = (
                    parse.clock_to_seconds(toi),
                    entry.get("shifts", 0),
                )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--team", default=config.TEAM)
    parser.add_argument("--season", default=config.SEASON)
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--game", type=int, default=None, help="limit to one game id")
    parser.add_argument("--goals", action="store_true",
                        help="print the on-ice five for every qualifying goal")
    parser.add_argument("--no-html-fallback", action="store_true")
    args = parser.parse_args(argv)

    fetcher = Fetcher(Path(args.cache_dir), verbose=False)
    team = args.team.upper()
    games, _ = parse.parse_schedule(fetcher.schedule(team, args.season))
    if args.game:
        games = [g for g in games if g.game_id == args.game]

    total_checked = 0
    total_mismatch = 0
    worst = 0
    # Independent rebuild of official +/- from play-by-play goals + shift data.
    rebuilt: dict[int, int] = {}
    official_pm: dict[int, int] = {}
    player_names: dict[int, str] = {}

    for ref in games:
        context = load_game_context(fetcher, ref, args.season,
                                    html_fallback=not args.no_html_fallback)
        timeline = build_timeline(context)
        if timeline is None:
            print(f"{ref.game_id}  SKIP (no shift rows from either source)")
            continue

        official = boxscore_toi(fetcher, ref.game_id)
        shift_counts: dict[int, int] = {}
        for shift in context.shifts:
            shift_counts[shift.player_id] = shift_counts.get(shift.player_id, 0) + 1

        mismatches: list[str] = []
        checked = 0
        for player_id, seconds in timeline.occupancy.items():
            if player_id not in official:
                continue
            checked += 1
            official_toi, official_shifts = official[player_id]
            delta = len(seconds) - official_toi
            worst = max(worst, abs(delta))
            if abs(delta) > TOLERANCE_SECONDS:
                player = context.roster.get(player_id)
                mismatches.append(
                    f"    {player.name if player else player_id:<22} "
                    f"mine {len(seconds):>4}s  official {official_toi:>4}s  "
                    f"delta {delta:+d}s  (shifts mine {shift_counts.get(player_id, 0)}"
                    f" / official {official_shifts})"
                )

        total_checked += checked
        total_mismatch += len(mismatches)
        status = "OK" if not mismatches else f"{len(mismatches)} MISMATCH"
        print(f"{ref.game_id} {ref.date} {ref.away_abbrev}@{ref.home_abbrev}  "
              f"players {checked:>2}  clean5v5 {timeline.clean_5v5_minutes:6.2f} min  "
              f"{context.shift_source:<11} [{status}]")
        for line in mismatches:
            print(line)

        # Rebuild official +/- for the club's skaters from first principles.
        team_id = context.team_id_for(team)
        if team_id is not None:
            club_skaters = {pid for pid, p in context.roster.items()
                            if p.team_id == team_id and not p.is_goalie}
            for pid in club_skaters:
                player_names[pid] = context.roster[pid].name
            for pid, value in stats_mod.official_plus_minus(
                    fetcher.boxscore(ref.game_id), context, team_id).items():
                official_pm[pid] = official_pm.get(pid, 0) + value
            for goal in context.goals:
                if goal.is_shootout:
                    continue
                eligible = stats_mod.counts_for_plus_minus(
                    goal.situation_code, goal.scoring_team_id == context.home_id)
                if not eligible:
                    continue
                delta = 1 if goal.scoring_team_id == team_id else -1
                for pid in timeline.on_ice_at(goal.absolute_second, club_skaters):
                    rebuilt[pid] = rebuilt.get(pid, 0) + delta

        if args.goals:
            team_id = context.team_id_for(team)
            skaters = {pid for pid, p in context.roster.items()
                       if p.team_id == team_id and not p.is_goalie}
            for goal in context.goals:
                if goal.is_shootout or not goal.is_clean_5v5_code:
                    continue
                on_ice = timeline.on_ice_at(goal.absolute_second, skaters)
                names = sorted(context.roster[p].name for p in on_ice)
                side = "GF" if goal.scoring_team_id == team_id else "GA"
                scorer = context.roster.get(goal.scorer_id)
                print(f"    {side} P{goal.period} {goal.time_in_period} "
                      f"by {scorer.name if scorer else goal.scorer_id} "
                      f"[{goal.situation_code}] -> {team} on ice: {', '.join(names)}")

    pm_bad = [
        (player_names.get(pid, pid), rebuilt.get(pid, 0), official_pm.get(pid, 0))
        for pid in set(rebuilt) | set(official_pm)
        if rebuilt.get(pid, 0) != official_pm.get(pid, 0)
    ]

    print()
    print(f"Player-games compared against official TOI : {total_checked}")
    print(f"Outside +/-{TOLERANCE_SECONDS}s tolerance              : {total_mismatch}")
    print(f"Largest absolute difference seen            : {worst}s")
    print()
    print(f"{team} skaters with official +/- rebuilt      : "
          f"{len(set(rebuilt) | set(official_pm))}")
    print(f"Rebuilt +/- disagreeing with the boxscore   : {len(pm_bad)}")
    for name, mine, theirs in pm_bad:
        print(f"    {name:<22} rebuilt {mine:+d}  boxscore {theirs:+d}")
    return 0 if not total_mismatch and not pm_bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
