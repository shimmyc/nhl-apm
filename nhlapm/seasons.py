"""Which season is current, and the facts about any season, from the NHL itself.

Nothing in the tool hard-codes a season id. The stats API's `season` table
lists every season with its regular-season start and end dates and its
scheduled game count; the current season is the newest one whose regular
season has started (UTC date). So:

* during a season, and through the playoffs and summer after it, the
  current season is that season -- the daily run keeps refreshing a finished
  season's file (a no-op once every game is in);
* on the day the next regular season starts, the daily run rolls into it on
  its own. Until its first game goes final there is nothing to publish, and
  update.py says so and exits cleanly rather than blocking.

    python -m nhlapm.seasons                  # print the current season id
    python -m nhlapm.seasons --github-output  # also write id=... for Actions
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .fetch import Fetcher


@dataclass(frozen=True)
class SeasonInfo:
    season: str
    start: str          # regular-season start, YYYY-MM-DD
    end: str            # regular-season end, YYYY-MM-DD
    games_per_team: int
    total_games: int    # scheduled regular-season games, league-wide


def all_seasons(fetcher: Fetcher) -> dict[str, SeasonInfo]:
    out: dict[str, SeasonInfo] = {}
    for row in fetcher.seasons().get("data", []):
        sid = str(row.get("id", ""))
        start = str(row.get("startDate") or "")[:10]
        if len(sid) != 8 or not start:
            continue
        out[sid] = SeasonInfo(
            season=sid, start=start,
            end=str(row.get("regularSeasonEndDate") or "")[:10],
            games_per_team=int(row.get("numberOfGames") or 0),
            total_games=int(row.get("totalRegularSeasonGames") or 0))
    return out


def current_season(fetcher: Fetcher, today: date | None = None) -> str:
    """The newest season whose regular season has started on or before today."""
    today_iso = (today or datetime.now(timezone.utc).date()).isoformat()
    started = [s for s in all_seasons(fetcher).values() if s.start <= today_iso]
    if not started:
        raise RuntimeError("the NHL season table lists no season that has started")
    return max(started, key=lambda s: s.season).season


def season_info(fetcher: Fetcher, season: str) -> SeasonInfo:
    info = all_seasons(fetcher).get(season)
    if info is None:
        raise RuntimeError(f"season {season} is not in the NHL season table")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", default=str(Path(__file__).resolve().parent.parent / "cache"))
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args(argv)
    season = current_season(Fetcher(Path(args.cache_dir), verbose=False))
    print(season)
    target = os.environ.get("GITHUB_OUTPUT")
    if args.github_output and target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"id={season}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
