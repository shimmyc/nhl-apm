"""Aggregate clean-5v5 time and goals into per-player adjusted plus-minus."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .parse import GameContext, Goal
from .timeline import GameTimeline

SECONDS_PER_HOUR = 3600.0


@dataclass
class PlayerAccumulator:
    player_id: int
    name: str
    position: str
    toi_seconds: int = 0
    gf_on: int = 0
    ga_on: int = 0
    official_pm: int = 0
    games: set[int] = field(default_factory=set)


@dataclass
class GameSummary:
    game_id: int
    label: str
    clean_5v5_seconds: int
    gf: int
    ga: int
    source: str = config.SOURCE_SHIFTCHARTS


@dataclass
class Diagnostics:
    """Cross-checks between the two independent feeds.

    situationCode comes from the play-by-play; the on-ice state comes from the
    shift charts. They are built by different systems, so where they disagree is
    worth surfacing rather than hiding behind an average.
    """
    code_1551_not_clean_in_shifts: list[str] = field(default_factory=list)
    clean_in_shifts_but_other_code: list[str] = field(default_factory=list)
    wrong_skater_count_on_ice: list[str] = field(default_factory=list)
    scorer_not_on_ice: list[str] = field(default_factory=list)
    missing_from_boxscore: list[str] = field(default_factory=list)
    shootout_goals_excluded: int = 0
    non_1551_goals_excluded: int = 0
    toi_identity_ok: bool = True
    toi_identity_detail: str = ""


@dataclass
class SeasonStats:
    team: str
    players: dict[int, PlayerAccumulator] = field(default_factory=dict)
    team_toi_seconds: int = 0
    team_gf: int = 0
    team_ga: int = 0
    games: list[GameSummary] = field(default_factory=list)
    diagnostics: Diagnostics = field(default_factory=Diagnostics)

    @property
    def team_gd(self) -> int:
        return self.team_gf - self.team_ga


def qualifying_goals(goals: list[Goal], diagnostics: Diagnostics) -> list[Goal]:
    """Goals at 5v5 with both goalies in net, shootout excluded."""
    keep: list[Goal] = []
    for goal in goals:
        if goal.is_shootout:
            diagnostics.shootout_goals_excluded += 1
            continue
        if not goal.is_clean_5v5_code:
            diagnostics.non_1551_goals_excluded += 1
            continue
        keep.append(goal)
    return keep


def official_plus_minus(boxscore: dict, context: GameContext, team_id: int) -> dict[int, int]:
    """playerId -> official +/- for the club's skaters in this game.

    The NHL's own number, all situations. It excludes power-play goals for the
    scoring team but counts shorthanded and empty-net goals, so it is the right
    baseline to difference against a strictly-5v5 figure.
    """
    side = "homeTeam" if team_id == context.home_id else "awayTeam"
    by_player: dict[int, int] = {}
    groups = boxscore.get("playerByGameStats", {}).get(side, {})
    for group in ("forwards", "defense"):
        for entry in groups.get(group, []):
            value = entry.get("plusMinus")
            if value is not None:
                by_player[entry["playerId"]] = value
    return by_player


def counts_for_plus_minus(situation_code: str, scoring_team_is_home: bool) -> bool | None:
    """Whether a goal earns official +/- for the players on the ice.

    The NHL awards plus/minus for even-strength and shorthanded goals, not for
    power-play goals. The test must compare TOTAL players per side -- skaters
    plus goalie -- rather than skaters alone: a team that has pulled its goalie
    ices six skaters without holding a man advantage, and a goal it scores does
    count. Comparing skater counts instead reads that as a power play and
    silently drops a real plus.

    Returns None when the situation code is not the expected four digits.
    """
    if len(situation_code) != 4 or not situation_code.isdigit():
        return None
    away_total = int(situation_code[0]) + int(situation_code[1])
    home_total = int(situation_code[2]) + int(situation_code[3])
    scoring, defending = ((home_total, away_total) if scoring_team_is_home
                          else (away_total, home_total))
    return scoring <= defending


def process_game(stats: SeasonStats, context: GameContext, timeline: GameTimeline,
                 boxscore: dict | None = None) -> GameSummary:
    """Fold one game's clean-5v5 time and goals into the season accumulators."""
    team_id = context.team_id_for(stats.team)
    if team_id is None:
        raise ValueError(f"{stats.team} does not appear in game {context.ref.game_id}")

    skater_ids = {
        pid for pid, player in context.roster.items()
        if player.team_id == team_id and not player.is_goalie
    }

    # Time on ice at clean 5v5.
    for player_id in skater_ids:
        seconds = timeline.occupancy.get(player_id)
        if not seconds:
            continue
        player = context.roster[player_id]
        accumulator = stats.players.get(player_id)
        if accumulator is None:
            accumulator = PlayerAccumulator(player_id, player.name, player.position)
            stats.players[player_id] = accumulator
        accumulator.name = player.name
        accumulator.position = player.position
        accumulator.games.add(context.ref.game_id)
        accumulator.toi_seconds += len(seconds & timeline.clean_5v5)

    # Official +/- over exactly the games that made it into this calculation.
    if boxscore is not None:
        official = official_plus_minus(boxscore, context, team_id)
        for player_id in skater_ids:
            if player_id not in timeline.occupancy:
                continue  # dressed but never on the ice
            accumulator = stats.players.get(player_id)
            if accumulator is None:
                continue
            if player_id in official:
                accumulator.official_pm += official[player_id]
            else:
                player = context.roster.get(player_id)
                stats.diagnostics.missing_from_boxscore.append(
                    f"{context.ref.game_id} {player.name if player else player_id}")

    stats.team_toi_seconds += timeline.clean_5v5_seconds

    # Goals.
    game_gf = game_ga = 0
    for goal in qualifying_goals(context.goals, stats.diagnostics):
        second = goal.absolute_second
        state = timeline.state_at(second)
        if state != (1, 5, 5, 1):
            stats.diagnostics.code_1551_not_clean_in_shifts.append(
                f"{context.ref.game_id} P{goal.period} {goal.time_in_period} "
                f"code 1551 but shifts show {''.join(map(str, state))}"
            )

        on_ice = timeline.on_ice_at(second, skater_ids)
        if len(on_ice) != 5:
            stats.diagnostics.wrong_skater_count_on_ice.append(
                f"{context.ref.game_id} P{goal.period} {goal.time_in_period} "
                f"{len(on_ice)} {stats.team} skater(s) on ice for a 1551 goal"
            )

        # The two feeds must agree on the most basic fact available: the player
        # the play-by-play credits with the goal has to be on the ice according
        # to the shift charts.
        if goal.scorer_id is not None:
            if goal.absolute_second not in timeline.occupancy.get(goal.scorer_id, ()):
                scorer = context.roster.get(goal.scorer_id)
                stats.diagnostics.scorer_not_on_ice.append(
                    f"{context.ref.game_id} P{goal.period} {goal.time_in_period} "
                    f"scorer {scorer.name if scorer else goal.scorer_id} "
                    f"absent from shift data at that second"
                )

        is_for = goal.scoring_team_id == team_id
        if is_for:
            game_gf += 1
        else:
            game_ga += 1
        for player_id in on_ice:
            accumulator = stats.players.get(player_id)
            if accumulator is None:
                player = context.roster[player_id]
                accumulator = PlayerAccumulator(player_id, player.name, player.position)
                stats.players[player_id] = accumulator
            if is_for:
                accumulator.gf_on += 1
            else:
                accumulator.ga_on += 1

    # The reverse check: a goal the shifts call clean 5v5 that the pbp does not.
    for goal in context.goals:
        if goal.is_shootout or goal.is_clean_5v5_code:
            continue
        if timeline.state_at(goal.absolute_second) == (1, 5, 5, 1):
            stats.diagnostics.clean_in_shifts_but_other_code.append(
                f"{context.ref.game_id} P{goal.period} {goal.time_in_period} "
                f"code {goal.situation_code} but shifts show 1551"
            )

    stats.team_gf += game_gf
    stats.team_ga += game_ga

    summary = GameSummary(
        game_id=context.ref.game_id,
        label=context.ref.label(),
        clean_5v5_seconds=timeline.clean_5v5_seconds,
        gf=game_gf,
        ga=game_ga,
        source=context.shift_source,
    )
    stats.games.append(summary)
    return summary


def verify_toi_identity(stats: SeasonStats) -> None:
    """Five skaters are on the ice for every clean-5v5 second, by definition.

    Summed skater TOI must therefore be exactly 5x team elapsed time. If it is
    not, the timeline and the aggregation disagree and the numbers are wrong.
    """
    total = sum(p.toi_seconds for p in stats.players.values())
    expected = 5 * stats.team_toi_seconds
    stats.diagnostics.toi_identity_ok = total == expected
    stats.diagnostics.toi_identity_detail = (
        f"sum(skater TOI)={total}s vs 5 x team elapsed={expected}s"
    )


@dataclass
class PlayerRow:
    name: str
    player_id: int
    position: str
    games_played: int
    toi_5v5: float
    gf_on: int
    ga_on: int
    gd_on: int
    official_pm: int
    nonclean_pm: int
    team_toi_5v5: float
    team_gd_5v5: int
    share: float
    expected: float
    adjusted: float
    gd_off: int
    toi_off: float
    rel: float | None
    gd_on_per_60: float | None
    team_gd_per_60: float | None


def build_rows(stats: SeasonStats) -> list[PlayerRow]:
    team_toi_seconds = stats.team_toi_seconds
    team_gd = stats.team_gd
    team_toi_minutes = team_toi_seconds / 60.0
    team_gd_per_60 = (
        team_gd * SECONDS_PER_HOUR / team_toi_seconds if team_toi_seconds else None
    )

    rows: list[PlayerRow] = []
    for accumulator in stats.players.values():
        toi_seconds = accumulator.toi_seconds
        gd_on = accumulator.gf_on - accumulator.ga_on
        share = toi_seconds / team_toi_seconds if team_toi_seconds else 0.0
        expected = team_gd * share
        off_seconds = team_toi_seconds - toi_seconds
        gd_off = team_gd - gd_on

        rel = gd_on - gd_off * (toi_seconds / off_seconds) if off_seconds else None
        gd_on_per_60 = (
            gd_on * SECONDS_PER_HOUR / toi_seconds if toi_seconds else None
        )

        rows.append(PlayerRow(
            name=accumulator.name,
            player_id=accumulator.player_id,
            position=accumulator.position,
            games_played=len(accumulator.games),
            toi_5v5=toi_seconds / 60.0,
            gf_on=accumulator.gf_on,
            ga_on=accumulator.ga_on,
            gd_on=gd_on,
            official_pm=accumulator.official_pm,
            nonclean_pm=accumulator.official_pm - gd_on,
            team_toi_5v5=team_toi_minutes,
            team_gd_5v5=team_gd,
            share=share,
            expected=expected,
            adjusted=gd_on - expected,
            gd_off=gd_off,
            toi_off=off_seconds / 60.0,
            rel=rel,
            gd_on_per_60=gd_on_per_60,
            team_gd_per_60=team_gd_per_60,
        ))

    rows.sort(key=lambda row: row.adjusted, reverse=True)
    return rows
