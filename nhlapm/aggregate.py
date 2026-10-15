"""Fold player-game rows into one line per skater.

This is the Python twin of the page's JavaScript aggregation: same sums, same
derived columns. The CLI, the CSV and the checks use it; the page does the
identical fold client-side over whatever rows its filters select.

Every input is a sum over the selected games, so a filter never needs a
special case:

    Expected = sum(Expected_game)
    Adjusted = GD_on - Expected
    Rel      = GD_on - GD_off x (TOI_on / TOI_off)    off-ice summed over the
                                                       same games the skater played
"""

from __future__ import annotations

from typing import Iterable

from .records import PlayerGame
from .stats import PlayerRow

SECONDS_PER_HOUR = 3600.0


def team_label(rows: list[PlayerGame]) -> str:
    """'CHI', or 'CHI/NYR' for a skater traded mid-selection (chronological)."""
    order: list[str] = []
    for row in sorted(rows, key=lambda r: (r.date, r.game_id)):
        if row.team not in order:
            order.append(row.team)
    return "/".join(order)


def aggregate(rows: Iterable[PlayerGame], players: dict[int, list] | None = None,
              fallback_names: dict[int, tuple[str, str]] | None = None) -> list[PlayerRow]:
    players = players or {}
    fallback_names = fallback_names or {}
    by_player: dict[int, list[PlayerGame]] = {}
    for row in rows:
        by_player.setdefault(row.player_id, []).append(row)

    out: list[PlayerRow] = []
    for pid, games in by_player.items():
        toi = sum(r.toi_5v5 for r in games)
        gf = sum(r.gf_on for r in games)
        ga = sum(r.ga_on for r in games)
        pm = sum(r.official_pm for r in games)
        expected = sum(r.expected_game for r in games)
        team_toi = sum(r.team_toi_5v5 for r in games)
        team_gd = sum(r.team_gd for r in games)
        toi_off = sum(r.toi_off for r in games)
        gd_off = sum(r.gd_off for r in games)
        gd_on = gf - ga

        meta = players.get(pid)
        if meta:
            name, position = meta[0], meta[1]
        else:
            name, position = fallback_names.get(pid, (f"player {pid}", "?"))

        out.append(PlayerRow(
            name=name, player_id=pid, position=position,
            games_played=len(games),
            toi_5v5=toi / 60.0, gf_on=gf, ga_on=ga, gd_on=gd_on,
            official_pm=pm, nonclean_pm=pm - gd_on,
            team_toi_5v5=team_toi / 60.0, team_gd_5v5=team_gd,
            share=toi / team_toi if team_toi else 0.0,
            expected=expected, adjusted=gd_on - expected,
            gd_off=gd_off, toi_off=toi_off / 60.0,
            rel=gd_on - gd_off * (toi / toi_off) if toi_off else None,
            gd_on_per_60=gd_on * SECONDS_PER_HOUR / toi if toi else None,
            team_gd_per_60=team_gd * SECONDS_PER_HOUR / team_toi if team_toi else None,
            team=team_label(games),
            adjusted_per_60=(gd_on - expected) * SECONDS_PER_HOUR / toi if toi else None,
        ))
    out.sort(key=lambda r: r.adjusted, reverse=True)
    return out
