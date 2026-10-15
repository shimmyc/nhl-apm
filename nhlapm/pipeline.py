"""Run the calculation for the whole league (or one club) and hand back a Season.

The CLI (`main.py`), the verifier (`verify.py`) and the daily job (`update.py`)
all go through `process_game`, so a number on the web page is produced by
exactly the code that produced the number in the terminal.

Incremental by construction, twice over:

* the processed season file is the record of finished work -- a game already
  in it is reused, not recomputed, so a day with three new games touches three
  games;
* `Fetcher` caches every raw response, so recomputing (`--rebuild`) or
  resuming an interrupted backfill only reaches the network for payloads it
  has never seen.

Quarantined games are retried every run with their raw payloads refetched, so
a game the NHL corrects later rejoins without anyone intervening.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timezone

from . import checks, config, parse
from .fetch import Fetcher, FetchError
from .loader import html_context, load_game_context
from .parse import GameRef
from .records import GameRecord, compute_game
from .store import Excluded, Season
from .timeline import build_timeline


@dataclass
class GameOutcome:
    ref: GameRef
    record: GameRecord | None = None
    reason: str | None = None          # set when the game is quarantined
    verdict: checks.GameVerdict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.record is not None and self.reason is None


def _evaluate(context, boxscore: dict) -> tuple[GameRecord | None,
                                               checks.GameVerdict | None, str | None]:
    """(record, verdict, reason-if-unusable) for one shift source."""
    if not context.shifts:
        return None, None, "no shift rows"
    timeline = build_timeline(context)
    if timeline is None:
        return None, None, "no usable shift intervals"
    if timeline.issues:
        return None, None, "; ".join(timeline.issues)
    if timeline.clean_5v5_minutes < config.MIN_PLAUSIBLE_5V5_MINUTES:
        return None, None, (f"only {timeline.clean_5v5_minutes:.2f} clean 5v5 minutes "
                            f"(below the {config.MIN_PLAUSIBLE_5V5_MINUTES:.0f} min floor)")
    record = compute_game(context, timeline, boxscore)
    verdict = checks.verify_game(context, timeline, boxscore, record)
    if verdict.boundary:
        record.notes["pm_line_change_on_goal_second"] = list(verdict.boundary)
    return record, verdict, (None if verdict.ok else "; ".join(verdict.failures))


def process_game(fetcher: Fetcher, ref: GameRef, season: str, *,
                 html_fallback: bool = True) -> GameOutcome:
    """Fetch, build, compute and verify one game. Never raises for bad data.

    Sources are tried in order -- shiftcharts, then the HTML TOI reports --
    and the first that passes every per-game check is used. Both face the
    same checks against the boxscore, so the fallback adds a second chance
    for the data, not a weaker bar.
    """
    outcome = GameOutcome(ref=ref)
    try:
        context = load_game_context(fetcher, ref, season, html_fallback=html_fallback)
    except FetchError as exc:
        outcome.reason = f"fetch failed: {exc}"
        return outcome
    except (KeyError, ValueError, TypeError) as exc:
        outcome.reason = f"could not parse payloads: {exc!r}"
        return outcome
    outcome.notes = list(context.shift_warnings)

    if not context.shifts:
        outcome.reason = ("no shift data from either source; "
                          + "; ".join(context.shift_warnings))
        return outcome
    try:
        boxscore = fetcher.boxscore(ref.game_id)
    except FetchError as exc:
        outcome.reason = f"boxscore unavailable: {exc}"
        return outcome

    record, verdict, reason = _evaluate(context, boxscore)
    if reason and html_fallback and context.shift_source == config.SOURCE_SHIFTCHARTS:
        alt, warnings = html_context(fetcher, context, season)
        if alt is not None:
            alt_record, alt_verdict, alt_reason = _evaluate(alt, boxscore)
            if alt_reason is None:
                outcome.notes.append(f"shiftcharts failed verification ({reason}); "
                                     f"HTML TOI reports used and verified")
                record, verdict, reason = alt_record, alt_verdict, None
            else:
                reason = f"shiftcharts: {reason} | HTML reports: {alt_reason}"
        else:
            reason = f"{reason} | HTML reports: {'; '.join(warnings) or 'unavailable'}"

    outcome.record, outcome.verdict, outcome.reason = record, verdict, reason
    return outcome


# -- discovery -------------------------------------------------------------

def discover_league_games(fetcher: Fetcher, season: str,
                          today: date_cls | None = None) -> tuple[list[GameRef], int]:
    """Every completed regular-season game this season, league-wide.

    Walks the weekly schedule from the season's first day. Settled weeks come
    from the cache, so a daily run makes one or two schedule requests.
    """
    today = today or datetime.now(timezone.utc).date()
    first = fetcher.schedule_week(today.isoformat())
    start = first.get("regularSeasonStartDate") or today.isoformat()
    season_end = first.get("regularSeasonEndDate") or ""

    games: dict[int, GameRef] = {}
    pending = 0
    cursor = start
    seen_weeks = set()
    while cursor and cursor not in seen_weeks and cursor <= today.isoformat():
        seen_weeks.add(cursor)
        payload = first if cursor == today.isoformat() else fetcher.schedule_week(cursor)
        completed, unplayed = parse.parse_schedule_week(payload)
        for ref in completed:
            if str(ref.game_id).startswith(season[:4]):
                games[ref.game_id] = ref
        pending += unplayed
        cursor = payload.get("nextStartDate")
        if season_end and cursor and cursor > season_end:
            break
    ordered = sorted(games.values(), key=lambda g: (g.date, g.game_id))
    return ordered, pending


def discover_team_games(fetcher: Fetcher, team: str, season: str) -> tuple[list[GameRef], int]:
    return parse.parse_schedule(fetcher.schedule(team, season))


# -- metadata --------------------------------------------------------------

def team_metadata(standings: dict) -> tuple[dict[str, tuple[str, str, str]],
                                            dict[str, str], dict[str, str], dict[str, int]]:
    """(abbr -> (name, conf, div), conferences, divisions, abbr -> gamesPlayed)."""
    teams, confs, divs, played = {}, {}, {}, {}
    for entry in standings.get("standings", []):
        abbr = (entry.get("teamAbbrev") or {}).get("default")
        if not abbr:
            continue
        conf, div = entry.get("conferenceAbbrev", ""), entry.get("divisionAbbrev", "")
        teams[abbr] = ((entry.get("teamName") or {}).get("default", abbr), conf, div)
        confs[conf] = entry.get("conferenceName", conf)
        divs[div] = entry.get("divisionName", div)
        played[abbr] = entry.get("gamesPlayed", 0)
    return teams, confs, divs, played


def player_metadata(bios: dict) -> dict[int, list]:
    """player id -> [name, position, shoots, birthdate] from the skater bios report."""
    out = {}
    for entry in bios.get("data", []):
        out[entry["playerId"]] = [entry.get("skaterFullName", ""),
                                  entry.get("positionCode", "?"),
                                  entry.get("shootsCatches") or "",
                                  entry.get("birthDate") or ""]
    return out


# -- the league run --------------------------------------------------------

@dataclass
class LeagueRun:
    season: Season
    pending_games: int = 0
    reused: int = 0
    computed: list[int] = field(default_factory=list)
    newly_quarantined: list[Excluded] = field(default_factory=list)
    recovered: list[int] = field(default_factory=list)
    standings_games: dict[str, int] | None = None
    roster_names: dict[int, tuple[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics: dict[str, list[str]] = field(default_factory=dict)
    fallbacks: list[str] = field(default_factory=list)


def run_league(fetcher: Fetcher, season_id: str, previous: Season | None, *,
               rebuild: bool = False, html_fallback: bool = True,
               refetch_quarantined: bool = True, log=print) -> LeagueRun:
    games, pending = discover_league_games(fetcher, season_id)
    season = Season(season=season_id)
    run = LeagueRun(season=season, pending_games=pending)
    previously_excluded = {e.game_id for e in (previous.excluded if previous else [])}

    log(f"  schedule: {len(games)} completed regular-season games, {pending} not yet played")
    for index, ref in enumerate(games, 1):
        if previous and not rebuild and ref.game_id in previous.games:
            entry = previous.games[ref.game_id]
            season.games[ref.game_id] = entry
            season.rows.extend(previous.rows_for_game(ref.game_id))
            run.reused += 1
            continue

        if ref.game_id in previously_excluded and refetch_quarantined and not rebuild:
            # The cached payloads are what failed; ask the NHL again.
            fetcher.drop_game_cache(ref.game_id)
        was_cached = fetcher.game_payloads_cached(ref.game_id)
        outcome = process_game(fetcher, ref, season_id, html_fallback=html_fallback)
        run.computed.append(ref.game_id)
        if outcome.ok:
            for key, items in outcome.record.notes.items():
                run.diagnostics.setdefault(key, []).extend(items)
            season.add_record(outcome.record)
            if ref.game_id in previously_excluded:
                run.recovered.append(ref.game_id)
            fallback = [n for n in outcome.notes if "failed verification" in n]
            if fallback:
                run.fallbacks.append(f"{ref.game_id}: {fallback[0]}")
            status = "ok" + (" (HTML reports; shiftcharts failed verification)" if fallback else "")
        else:
            item = Excluded(ref.game_id, ref.date, ref.away_abbrev, ref.home_abbrev,
                            outcome.reason or "unknown")
            season.excluded.append(item)
            if ref.game_id not in previously_excluded:
                run.newly_quarantined.append(item)
            status = f"QUARANTINED: {outcome.reason}"
        log(f"  [{index:>4}/{len(games)}] {ref.game_id} {ref.date} "
            f"{ref.away_abbrev}@{ref.home_abbrev} "
            f"{'cache' if was_cached else 'fetch'}  {status}")

    # Metadata: three requests, refetched every run.
    standings = fetcher.standings()
    season.teams, season.conferences, season.divisions, run.standings_games = \
        team_metadata(standings)
    rookies = fetcher.rookies(season_id)
    season.rookies = {entry["playerId"] for entry in rookies.get("data", [])}
    return run


def collect_player_metadata(fetcher: Fetcher, season_id: str, season: Season,
                            existing: dict[int, list]) -> tuple[dict[int, list], list[int]]:
    """Merge this season's skater bios into the shared lookup.

    Returns (players, ids still missing). Missing ids -- a skater the bios
    report has not caught up with yet -- are filled from the play-by-play
    roster so the page always has a name and position to show.
    """
    players = dict(existing)
    players.update(player_metadata(fetcher.skater_bios(season_id)))
    missing = sorted({r.player_id for r in season.rows} - set(players))
    return players, missing


def fill_from_rosters(fetcher: Fetcher, season: Season, players: dict[int, list],
                      missing: list[int]) -> None:
    """Name and position for skaters the bios report lacks, from cached pbp."""
    want = set(missing)
    if not want:
        return
    for row in season.rows:
        if row.player_id not in want:
            continue
        try:
            pbp = fetcher.play_by_play(row.game_id)
        except FetchError:
            continue
        roster = parse.parse_roster(pbp)
        for pid in list(want):
            if pid in roster:
                player = roster[pid]
                players[pid] = [player.name, player.position, "", ""]
                want.discard(pid)
        if not want:
            return
