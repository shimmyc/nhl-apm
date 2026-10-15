"""The stored unit of the whole system: one row per skater per game.

Every published number is a sum over these rows, so any filter -- a team, a
date range, home games only, a player's last ten -- is a selection followed by
a sum. Nothing is ratio-of-season: Expected is built game by game,

    Expected_game = team 5v5 GD in that game x (player 5v5 TOI / team 5v5 TOI)

and a season Expected is the sum of those. Per team per game the identities

    sum(skater TOI)    == 5 x team clean-5v5 seconds
    sum(GD_on)         == 5 x team 5v5 GD
    sum(Adjusted_game) == 0

hold exactly, so they hold for every union of games too.

Everything here is integers except Expected, which is a pure function of the
stored integers. The season file stores the integers and derives Expected on
load, so the file cannot drift from the definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, stats as stats_mod
from .parse import GameContext
from .timeline import GameTimeline

SIDE_AWAY = 0
SIDE_HOME = 1


@dataclass(frozen=True)
class PlayerGame:
    player_id: int
    game_id: int
    date: str
    team: str
    opponent: str
    is_home: bool
    toi_5v5: int          # seconds at clean 5v5
    gf_on: int
    ga_on: int
    official_pm: int      # the NHL's own +/- for this game, all situations
    team_toi_5v5: int     # the game's clean-5v5 seconds (same for both teams)
    team_gf: int          # this player's team, clean 5v5, this game
    team_ga: int

    @property
    def gd_on(self) -> int:
        return self.gf_on - self.ga_on

    @property
    def team_gd(self) -> int:
        return self.team_gf - self.team_ga

    @property
    def expected_game(self) -> float:
        if not self.team_toi_5v5:
            return 0.0
        return self.team_gd * self.toi_5v5 / self.team_toi_5v5

    # Off-ice components, the parts Rel needs. Additive like everything else.
    @property
    def toi_off(self) -> int:
        return self.team_toi_5v5 - self.toi_5v5

    @property
    def gd_off(self) -> int:
        return self.team_gd - self.gd_on


@dataclass
class GameRecord:
    """One game, both teams, ready to store."""
    game_id: int
    date: str
    home: str
    away: str
    clean_seconds: int
    home_gf: int           # home clean-5v5 goals
    away_gf: int           # away clean-5v5 goals
    source: str
    rows: list[PlayerGame] = field(default_factory=list)
    # Non-blocking feed disagreements, kept for the diagnostics summary.
    notes: dict[str, list[str]] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.game_id} {self.date} {self.away}@{self.home}"


def compute_game(context: GameContext, timeline: GameTimeline,
                 boxscore: dict) -> GameRecord:
    """Both teams' per-skater rows for one game."""
    diagnostics = stats_mod.Diagnostics()
    goals = stats_mod.qualifying_goals(context.goals, diagnostics)
    clean = timeline.clean_5v5

    home_gf = sum(1 for g in goals if g.scoring_team_id == context.home_id)
    away_gf = sum(1 for g in goals if g.scoring_team_id == context.away_id)

    record = GameRecord(
        game_id=context.ref.game_id, date=context.ref.date,
        home=context.home_abbrev, away=context.away_abbrev,
        clean_seconds=len(clean), home_gf=home_gf, away_gf=away_gf,
        source=context.shift_source,
    )
    notes = record.notes

    for team_id, is_home in ((context.away_id, False), (context.home_id, True)):
        team = context.home_abbrev if is_home else context.away_abbrev
        opponent = context.away_abbrev if is_home else context.home_abbrev
        team_gf, team_ga = (home_gf, away_gf) if is_home else (away_gf, home_gf)
        skaters = {pid for pid, p in context.roster.items()
                   if p.team_id == team_id and not p.is_goalie}
        official = stats_mod.official_plus_minus(boxscore, context, team_id)

        gf_on: dict[int, int] = {}
        ga_on: dict[int, int] = {}
        for goal in goals:
            on_ice = timeline.on_ice_at(goal.absolute_second, skaters)
            if len(on_ice) != 5:
                notes.setdefault("wrong_skater_count", []).append(
                    f"{context.ref.game_id} P{goal.period} {goal.time_in_period} "
                    f"{len(on_ice)} {team} skaters on ice for a 1551 goal")
            bucket = gf_on if goal.scoring_team_id == team_id else ga_on
            for pid in on_ice:
                bucket[pid] = bucket.get(pid, 0) + 1

        for pid in sorted(skaters):
            seconds = timeline.occupancy.get(pid)
            if not seconds:
                continue    # dressed, never on the ice: no game played
            if pid not in official:
                notes.setdefault("missing_from_boxscore", []).append(
                    f"{context.ref.game_id} {context.roster[pid].name}")
            record.rows.append(PlayerGame(
                player_id=pid, game_id=context.ref.game_id, date=context.ref.date,
                team=team, opponent=opponent, is_home=is_home,
                toi_5v5=len(seconds & clean),
                gf_on=gf_on.get(pid, 0), ga_on=ga_on.get(pid, 0),
                official_pm=official.get(pid, 0),
                team_toi_5v5=len(clean), team_gf=team_gf, team_ga=team_ga,
            ))

    # Feed cross-checks: situationCode versus the shift-derived state.
    for goal in context.goals:
        if goal.is_shootout:
            continue
        state = timeline.state_at(goal.absolute_second)
        where = f"{context.ref.game_id} P{goal.period} {goal.time_in_period}"
        if goal.is_clean_5v5_code and state != (1, 5, 5, 1):
            notes.setdefault("code_1551_not_clean", []).append(
                f"{where} code 1551 but shifts show {''.join(map(str, state))}")
        if not goal.is_clean_5v5_code and state == (1, 5, 5, 1):
            notes.setdefault("clean_but_other_code", []).append(
                f"{where} code {goal.situation_code} but shifts show 1551")
        if goal.is_clean_5v5_code and goal.scorer_id is not None and \
                goal.absolute_second not in timeline.occupancy.get(goal.scorer_id, ()):
            notes.setdefault("scorer_not_on_ice", []).append(
                f"{where} scorer {goal.scorer_id} absent from shift data")
    return record
