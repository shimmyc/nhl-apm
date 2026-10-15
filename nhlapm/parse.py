"""Turn raw API payloads into typed records.

All times become absolute game seconds on a single axis so that shift rows
(from the stats API) and goals (from api-web) are directly comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config


# -- time ------------------------------------------------------------------

def clock_to_seconds(value: str) -> int:
    """'MM:SS' (elapsed within a period) -> seconds."""
    minutes, seconds = value.split(":")
    return int(minutes) * 60 + int(seconds)


def absolute_seconds(period: int, clock: str) -> int:
    """Elapsed seconds since the opening faceoff, on the shared period grid."""
    return (period - 1) * config.PERIOD_BLOCK_SECONDS + clock_to_seconds(clock)


# -- records ---------------------------------------------------------------

@dataclass(frozen=True)
class GameRef:
    game_id: int
    date: str
    home_abbrev: str
    away_abbrev: str
    home_score: int
    away_score: int
    state: str

    def label(self) -> str:
        return (f"{self.game_id} {self.date} {self.away_abbrev} {self.away_score}"
                f" @ {self.home_abbrev} {self.home_score}")


@dataclass(frozen=True)
class Player:
    player_id: int
    name: str
    team_id: int
    team_abbrev: str
    position: str
    sweater: int | None = None
    last_name: str = ""   # kept separate: surnames like "Del Mastro" have spaces

    @property
    def is_goalie(self) -> bool:
        return self.position == config.GOALIE_POSITION_CODE


@dataclass(frozen=True)
class Goal:
    event_id: int
    period: int
    period_type: str
    time_in_period: str
    absolute_second: int
    situation_code: str
    scoring_team_id: int
    scorer_id: int | None
    goalie_in_net_id: int | None

    @property
    def is_clean_5v5_code(self) -> bool:
        return self.situation_code == config.EVEN_STRENGTH_5V5

    @property
    def is_shootout(self) -> bool:
        return self.period_type == config.SHOOTOUT_PERIOD_TYPE


@dataclass(frozen=True)
class Shift:
    player_id: int
    team_id: int
    team_abbrev: str
    period: int
    start: int          # absolute seconds, exclusive
    end: int            # absolute seconds, inclusive


@dataclass
class GameContext:
    """Everything parsed out of one game's two payloads."""
    ref: GameRef
    home_id: int
    away_id: int
    home_abbrev: str
    away_abbrev: str
    roster: dict[int, Player]
    goals: list[Goal]
    shifts: list[Shift]
    periods_in_play: set[int] = field(default_factory=set)
    shift_warnings: list[str] = field(default_factory=list)
    shift_source: str = config.SOURCE_SHIFTCHARTS

    def replace_shifts(self, shifts: list[Shift], source: str,
                       warnings: list[str] | None = None) -> None:
        """Swap in shifts from a fallback feed, recording where they came from."""
        self.shifts = shifts
        self.shift_source = source
        if warnings:
            self.shift_warnings = list(self.shift_warnings) + list(warnings)

    def team_id_for(self, abbrev: str) -> int | None:
        if abbrev == self.home_abbrev:
            return self.home_id
        if abbrev == self.away_abbrev:
            return self.away_id
        return None


# -- parsers ---------------------------------------------------------------

def parse_schedule(payload: dict, *, game_type: int = config.GAME_TYPE_REGULAR,
                   states: tuple[str, ...] = config.COMPLETED_GAME_STATES
                   ) -> tuple[list[GameRef], int]:
    """Return (completed games of the requested type, count of scheduled-but-unplayed)."""
    completed: list[GameRef] = []
    pending = 0
    for game in payload.get("games", []):
        if game.get("gameType") != game_type:
            continue
        home, away = game["homeTeam"], game["awayTeam"]
        if game.get("gameState") not in states:
            pending += 1
            continue
        completed.append(GameRef(
            game_id=game["id"],
            date=game.get("gameDate", ""),
            home_abbrev=home.get("abbrev", "?"),
            away_abbrev=away.get("abbrev", "?"),
            home_score=home.get("score", 0),
            away_score=away.get("score", 0),
            state=game.get("gameState", ""),
        ))
    completed.sort(key=lambda g: (g.date, g.game_id))
    return completed, pending


def _flat_name(spot: dict) -> str:
    first = (spot.get("firstName") or {}).get("default", "")
    last = (spot.get("lastName") or {}).get("default", "")
    return f"{first} {last}".strip() or f"player {spot.get('playerId')}"


def parse_roster(pbp: dict) -> dict[int, Player]:
    """playerId -> Player, with position taken from the play-by-play roster."""
    abbrev_by_team_id = {
        pbp["homeTeam"]["id"]: pbp["homeTeam"].get("abbrev", "HOME"),
        pbp["awayTeam"]["id"]: pbp["awayTeam"].get("abbrev", "AWAY"),
    }
    roster: dict[int, Player] = {}
    for spot in pbp.get("rosterSpots", []):
        team_id = spot["teamId"]
        roster[spot["playerId"]] = Player(
            player_id=spot["playerId"],
            name=_flat_name(spot),
            team_id=team_id,
            team_abbrev=abbrev_by_team_id.get(team_id, str(team_id)),
            position=spot.get("positionCode", "?"),
            sweater=spot.get("sweaterNumber"),
            last_name=(spot.get("lastName") or {}).get("default", ""),
        )
    return roster


def parse_goals(pbp: dict) -> list[Goal]:
    """Every goal event in the game, shootout included (callers filter)."""
    goals: list[Goal] = []
    for play in pbp.get("plays", []):
        if play.get("typeDescKey") != "goal":
            continue
        descriptor = play.get("periodDescriptor") or {}
        period = descriptor.get("number")
        clock = play.get("timeInPeriod")
        if period is None or not clock:
            continue
        details = play.get("details") or {}
        goals.append(Goal(
            event_id=play.get("eventId", -1),
            period=period,
            period_type=descriptor.get("periodType", "REG"),
            time_in_period=clock,
            absolute_second=absolute_seconds(period, clock),
            situation_code=str(play.get("situationCode", "")),
            scoring_team_id=details.get("eventOwnerTeamId", -1),
            scorer_id=details.get("scoringPlayerId"),
            goalie_in_net_id=details.get("goalieInNetId"),
        ))
    return goals


def periods_with_play(pbp: dict) -> set[int]:
    """Periods that actually have events, used to detect truncated shift data."""
    periods = set()
    for play in pbp.get("plays", []):
        descriptor = play.get("periodDescriptor") or {}
        if descriptor.get("periodType") == config.SHOOTOUT_PERIOD_TYPE:
            continue
        number = descriptor.get("number")
        if number is not None:
            periods.add(number)
    return periods


def parse_shifts(payload: dict, roster: dict[int, Player]
                 ) -> tuple[list[Shift], list[str]]:
    """Shift rows only -- goal/event rows in the same feed are discarded.

    Returns (shifts, warnings). Rows that cannot describe a real interval are
    dropped and reported rather than silently coerced.
    """
    shifts: list[Shift] = []
    warnings: list[str] = []
    dropped_event_rows = 0
    dropped_bad_interval = 0
    dropped_unknown_player = 0
    team_mismatch = 0

    for row in payload.get("data", []):
        if row.get("typeCode") != config.SHIFT_ROW_TYPE_CODE:
            dropped_event_rows += 1
            continue
        # Goal rows carry an eventDescription and no usable interval; belt and
        # braces alongside the type-code filter.
        if row.get("eventDescription"):
            dropped_event_rows += 1
            continue

        period = row.get("period")
        start_clock = row.get("startTime")
        end_clock = row.get("endTime")
        if period is None or not start_clock or not end_clock:
            dropped_bad_interval += 1
            continue

        start = absolute_seconds(period, start_clock)
        end = absolute_seconds(period, end_clock)

        if end <= start:
            # A shift that runs to the buzzer sometimes reports endTime 00:00.
            duration = row.get("duration")
            if duration:
                end = start + clock_to_seconds(duration)
            if end <= start:
                dropped_bad_interval += 1
                continue

        player_id = row.get("playerId")
        player = roster.get(player_id)
        if player is None:
            dropped_unknown_player += 1
            continue
        if row.get("teamId") != player.team_id:
            team_mismatch += 1

        shifts.append(Shift(
            player_id=player_id,
            team_id=player.team_id,
            team_abbrev=player.team_abbrev,
            period=period,
            start=start,
            end=end,
        ))

    if dropped_event_rows:
        warnings.append(f"{dropped_event_rows} non-shift row(s) filtered out")
    if dropped_bad_interval:
        warnings.append(f"{dropped_bad_interval} shift row(s) with unusable times dropped")
    if dropped_unknown_player:
        warnings.append(f"{dropped_unknown_player} shift row(s) for players absent from the pbp roster")
    if team_mismatch:
        warnings.append(f"{team_mismatch} shift row(s) whose teamId disagrees with the pbp roster")
    return shifts, warnings


def build_game_context(ref: GameRef, pbp: dict, shift_payload: dict) -> GameContext:
    roster = parse_roster(pbp)
    shifts, warnings = parse_shifts(shift_payload, roster)
    return GameContext(
        ref=ref,
        home_id=pbp["homeTeam"]["id"],
        away_id=pbp["awayTeam"]["id"],
        home_abbrev=pbp["homeTeam"].get("abbrev", "HOME"),
        away_abbrev=pbp["awayTeam"].get("abbrev", "AWAY"),
        roster=roster,
        goals=parse_goals(pbp),
        shifts=shifts,
        periods_in_play=periods_with_play(pbp),
        shift_warnings=warnings,
    )


def parse_schedule_week(payload: dict, *, game_type: int = config.GAME_TYPE_REGULAR,
                        states: tuple[str, ...] = config.COMPLETED_GAME_STATES
                        ) -> tuple[list[GameRef], int]:
    """League-wide weekly schedule -> (completed games, scheduled-but-unplayed count).

    The weekly feed carries the date on the day, not on the game, so the date
    is taken from the enclosing `gameWeek` entry.
    """
    completed: list[GameRef] = []
    pending = 0
    for day in payload.get("gameWeek", []):
        for game in day.get("games", []):
            if game.get("gameType") != game_type:
                continue
            if game.get("gameState") not in states:
                pending += 1
                continue
            home, away = game["homeTeam"], game["awayTeam"]
            completed.append(GameRef(
                game_id=game["id"],
                date=game.get("gameDate") or day.get("date", ""),
                home_abbrev=home.get("abbrev", "?"),
                away_abbrev=away.get("abbrev", "?"),
                home_score=home.get("score", 0),
                away_score=away.get("score", 0),
                state=game.get("gameState", ""),
            ))
    return completed, pending
