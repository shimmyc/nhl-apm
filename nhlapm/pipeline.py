"""Run the whole calculation for one club and hand back everything downstream needs.

Both the CLI (`main.py`) and the site builder (`update.py`) go through here, so
a number on the web page is produced by exactly the code that produced the
number in the terminal.

Fetching is incremental by construction: `Fetcher` caches every response
verbatim, so a rerun only reaches the network for games whose payloads are not
on disk yet. The schedule itself is always refetched -- it is one call, and it
is how new completed games are discovered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, stats as stats_mod
from .fetch import Fetcher, FetchError
from .loader import load_game_context
from .parse import GameRef
from .stats import PlayerRow, SeasonStats
from .timeline import build_timeline


@dataclass
class TeamRun:
    """The result of running the pipeline for one club."""
    team: str
    season: str
    stats: SeasonStats
    rows: list[PlayerRow] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    pending_games: int = 0
    notes: list[str] = field(default_factory=list)
    newly_fetched: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.stats.games)


def _cached_game_ids(fetcher: Fetcher, games: list[GameRef]) -> set[int]:
    """Games whose play-by-play is already on disk before this run starts."""
    return {
        game.game_id for game in games
        if (fetcher.cache_dir / f"pbp_{game.game_id}.json").exists()
    }


def run_team(fetcher: Fetcher, team: str, season: str, *,
             html_fallback: bool = True,
             retry_empty_shifts: bool = False,
             on_note=None) -> TeamRun:
    """Fetch, parse and aggregate one club's completed regular-season games."""
    from . import parse as parse_mod

    team = team.upper()
    run = TeamRun(team=team, season=season, stats=SeasonStats(team=team))

    def note(message: str) -> None:
        run.notes.append(message)
        if on_note is not None:
            on_note(message)

    try:
        schedule_payload = fetcher.schedule(team, season)
    except FetchError as exc:
        run.error = f"could not load the schedule: {exc}"
        return run

    games, pending = parse_mod.parse_schedule(schedule_payload)
    run.pending_games = pending
    if not games:
        return run

    already_cached = _cached_game_ids(fetcher, games)
    run.newly_fetched = [g.game_id for g in games if g.game_id not in already_cached]

    for game in games:
        if retry_empty_shifts and fetcher.shift_cache_is_empty(game.game_id):
            fetcher.drop_shift_cache(game.game_id)

        try:
            context = load_game_context(fetcher, game, season,
                                        html_fallback=html_fallback)
        except FetchError as exc:
            run.skipped.append((game.label(), f"fetch failed: {exc}"))
            continue
        except (KeyError, ValueError) as exc:
            run.skipped.append((game.label(), f"could not parse payloads: {exc!r}"))
            continue

        if not context.shifts:
            run.skipped.append((game.label(),
                                "no shift data from either source -- excluded from all "
                                "totals; " + "; ".join(context.shift_warnings)))
            continue

        timeline = build_timeline(context)
        if timeline is None:
            run.skipped.append((game.label(), "no usable shift intervals"))
            continue

        if timeline.issues:
            run.skipped.append((game.label(), "; ".join(timeline.issues)))
            continue

        if timeline.clean_5v5_minutes < config.MIN_PLAUSIBLE_5V5_MINUTES:
            run.skipped.append((
                game.label(),
                f"only {timeline.clean_5v5_minutes:.2f} clean 5v5 minutes found "
                f"(below the {config.MIN_PLAUSIBLE_5V5_MINUTES:.0f} min plausibility floor)"))
            continue

        try:
            boxscore = fetcher.boxscore(game.game_id)
        except FetchError as exc:
            run.skipped.append((game.label(), f"boxscore unavailable: {exc}"))
            continue

        try:
            stats_mod.process_game(run.stats, context, timeline, boxscore)
        except ValueError as exc:
            run.skipped.append((game.label(), str(exc)))
            continue

        if context.shift_warnings:
            note(f"  note {game.game_id}: {'; '.join(context.shift_warnings)}")

    if run.stats.games:
        stats_mod.verify_toi_identity(run.stats)
        run.rows = stats_mod.build_rows(run.stats)
    return run
