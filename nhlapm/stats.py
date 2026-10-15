"""Goal eligibility rules, the official +/- reader, and the output row type.

Aggregation itself lives in aggregate.py, over the per-game rows of records.py."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .parse import GameContext, Goal


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
    team: str = ""
    adjusted_per_60: float | None = None


def is_penalty_shot(situation_code: str) -> bool:
    """A lone shooter against a lone goalie, coded e.g. 1010.

    Fewer than three skaters a side never happens in open play, so that is the
    test. The NHL credits the shooter alone with the plus for a penalty-shot
    goal -- observed in game 2026020065, where Eichel's penalty-shot goal moved
    his published +/- and no one else's.
    """
    return (len(situation_code) == 4 and situation_code.isdigit()
            and (int(situation_code[1]) < 3 or int(situation_code[2]) < 3))
