"""Second-by-second on-ice state for one game.

Convention: second *t* denotes the interval (t-1, t]. A shift with absolute
bounds (start, end] therefore occupies seconds start+1 .. end. That is the same
half-open convention as the on-ice rule for goals -- shift_start < goal_time <=
shift_end -- so the set of players occupying second G is exactly the set on ice
for a goal scored at G. One convention, both uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .parse import GameContext, Player, Shift


@dataclass
class GameTimeline:
    total_seconds: int
    # occupancy[player_id] -> set of seconds that player was on the ice
    occupancy: dict[int, set[int]]
    clean_5v5: set[int]
    # diagnostic counts, indexed by second
    home_skaters: list[int]
    home_goalies: list[int]
    away_skaters: list[int]
    away_goalies: list[int]
    issues: list[str] = field(default_factory=list)

    @property
    def clean_5v5_seconds(self) -> int:
        return len(self.clean_5v5)

    @property
    def clean_5v5_minutes(self) -> float:
        return len(self.clean_5v5) / 60.0

    def state_at(self, second: int) -> tuple[int, int, int, int]:
        """(away goalies, away skaters, home skaters, home goalies) -- situationCode order."""
        if not 0 <= second < len(self.home_skaters):
            return (0, 0, 0, 0)
        return (self.away_goalies[second], self.away_skaters[second],
                self.home_skaters[second], self.home_goalies[second])

    def on_ice_at(self, second: int, player_ids: set[int]) -> set[int]:
        return {pid for pid in player_ids if second in self.occupancy.get(pid, ())}


def build_timeline(context: GameContext) -> GameTimeline | None:
    """None when there are no shift rows to build from."""
    shifts: list[Shift] = context.shifts
    if not shifts:
        return None

    total = max(shift.end for shift in shifts)

    # Collapse each player's shifts into a set of seconds first. Overlapping or
    # duplicated rows for one player must not inflate the team's skater count.
    occupancy: dict[int, set[int]] = {}
    for shift in shifts:
        occupancy.setdefault(shift.player_id, set()).update(
            range(shift.start + 1, shift.end + 1)
        )

    size = total + 1
    home_skaters = [0] * size
    home_goalies = [0] * size
    away_skaters = [0] * size
    away_goalies = [0] * size

    for player_id, seconds in occupancy.items():
        player: Player | None = context.roster.get(player_id)
        if player is None:
            continue
        if player.team_id == context.home_id:
            bucket = home_goalies if player.is_goalie else home_skaters
        elif player.team_id == context.away_id:
            bucket = away_goalies if player.is_goalie else away_skaters
        else:
            continue
        for second in seconds:
            if second < size:
                bucket[second] += 1

    clean = {
        second for second in range(1, size)
        if home_skaters[second] == 5 and home_goalies[second] == 1
        and away_skaters[second] == 5 and away_goalies[second] == 1
    }

    timeline = GameTimeline(
        total_seconds=total,
        occupancy=occupancy,
        clean_5v5=clean,
        home_skaters=home_skaters,
        home_goalies=home_goalies,
        away_skaters=away_skaters,
        away_goalies=away_goalies,
    )
    timeline.issues = _coverage_issues(context, timeline)
    return timeline


def _coverage_issues(context: GameContext, timeline: GameTimeline) -> list[str]:
    """Structural problems that mean the shift feed does not cover the game."""
    issues: list[str] = []

    periods_in_shifts = {shift.period for shift in context.shifts}
    missing = sorted(context.periods_in_play - periods_in_shifts)
    if missing:
        issues.append(f"no shift rows for period(s) {', '.join(map(str, missing))}")

    # A regulation period must be covered out to 20:00 by somebody.
    for period in sorted(periods_in_shifts):
        if period > config.REGULATION_PERIODS:
            continue  # OT length varies; not a fixed expectation
        latest = max(shift.end for shift in context.shifts if shift.period == period)
        expected_end = period * config.PERIOD_BLOCK_SECONDS
        if latest < expected_end:
            short_by = expected_end - latest
            issues.append(
                f"period {period} shift data ends {short_by}s early "
                f"({latest % config.PERIOD_BLOCK_SECONDS // 60:02d}:"
                f"{latest % 60:02d} of 20:00)"
            )

    # Dead seconds inside a covered period: nobody on the ice for a team.
    dead = 0
    for second in range(1, timeline.total_seconds + 1):
        period = (second - 1) // config.PERIOD_BLOCK_SECONDS + 1
        if period not in periods_in_shifts:
            continue
        if period <= config.REGULATION_PERIODS or second <= timeline.total_seconds:
            if (timeline.home_skaters[second] == 0 or timeline.away_skaters[second] == 0):
                dead += 1
    if dead > 0:
        pct = 100.0 * dead / max(timeline.total_seconds, 1)
        if pct >= 2.0:
            issues.append(f"{dead}s ({pct:.1f}%) with a team showing zero skaters on ice")

    return issues
