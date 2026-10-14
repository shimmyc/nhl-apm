"""CSV export, console table, and the validation summary."""

from __future__ import annotations

import csv
from pathlib import Path

from .stats import PlayerRow, SeasonStats

CSV_COLUMNS = [
    ("player", lambda r: r.name),
    ("player_id", lambda r: r.player_id),
    ("position", lambda r: r.position),
    ("gp", lambda r: r.games_played),
    ("toi_5v5", lambda r: round(r.toi_5v5, 2)),
    ("gf_on", lambda r: r.gf_on),
    ("ga_on", lambda r: r.ga_on),
    ("gd_on", lambda r: r.gd_on),
    ("official_pm", lambda r: r.official_pm),
    ("nonclean_pm", lambda r: r.nonclean_pm),
    ("team_toi_5v5", lambda r: round(r.team_toi_5v5, 2)),
    ("team_gd_5v5", lambda r: r.team_gd_5v5),
    ("share", lambda r: round(r.share, 4)),
    ("expected", lambda r: round(r.expected, 3)),
    ("adjusted", lambda r: round(r.adjusted, 3)),
    ("gd_off", lambda r: r.gd_off),
    ("toi_off", lambda r: round(r.toi_off, 2)),
    ("rel", lambda r: "" if r.rel is None else round(r.rel, 3)),
    ("gd_on_per_60", lambda r: "" if r.gd_on_per_60 is None else round(r.gd_on_per_60, 3)),
    ("team_gd_per_60", lambda r: "" if r.team_gd_per_60 is None else round(r.team_gd_per_60, 3)),
]


def write_csv(rows: list[PlayerRow], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([name for name, _ in CSV_COLUMNS])
        for row in rows:
            writer.writerow([getter(row) for _, getter in CSV_COLUMNS])
    return path


def _fmt(value: float | None, places: int = 2, plus: bool = False) -> str:
    if value is None:
        return "--"
    if plus:
        return f"{value:+.{places}f}"
    return f"{value:.{places}f}"


CONSOLE_HEADERS = ("PLAYER", "POS", "GP", "TOI_5v5", "GD_on", "OFF_PM", "NONCLEAN",
                   "EXPECTED", "ADJUSTED", "REL", "GD_on/60")
CONSOLE_WIDTHS = (21, 3, 3, 8, 6, 7, 8, 9, 9, 7, 9)


def _console_line(cells: tuple[str, ...]) -> str:
    out = [cells[0].ljust(CONSOLE_WIDTHS[0])[:CONSOLE_WIDTHS[0]],
           cells[1].center(CONSOLE_WIDTHS[1])]
    out.extend(cell.rjust(width) for cell, width in zip(cells[2:], CONSOLE_WIDTHS[2:]))
    return "  ".join(out)


def print_table(rows: list[PlayerRow], title: str) -> None:
    print()
    print(title)
    header = _console_line(CONSOLE_HEADERS)
    print(header)
    print("-" * len(header))
    for row in rows:
        print(_console_line((
            row.name,
            row.position,
            str(row.games_played),
            _fmt(row.toi_5v5, 1),
            f"{row.gd_on:+d}",
            f"{row.official_pm:+d}",
            f"{row.nonclean_pm:+d}",
            _fmt(row.expected, 2, plus=True),
            _fmt(row.adjusted, 2, plus=True),
            _fmt(row.rel, 2, plus=True),
            _fmt(row.gd_on_per_60, 2, plus=True),
        )))
    print("-" * len(header))


def print_validation(stats: SeasonStats, rows: list[PlayerRow],
                     skipped: list[tuple[str, str]], pending_games: int) -> None:
    diagnostics = stats.diagnostics
    print()
    print("=" * 78)
    print("VALIDATION SUMMARY")
    print("=" * 78)

    print(f"Games with usable shift data : {len(stats.games)}")
    print(f"Games skipped                : {len(skipped)}")
    print(f"Games not yet played         : {pending_games}")
    print(f"Team 5v5 GF / GA             : {stats.team_gf} / {stats.team_ga}"
          f"  (GD {stats.team_gd:+d})")
    print(f"Team 5v5 minutes             : {stats.team_toi_seconds / 60:.2f}"
          f"  ({stats.team_toi_seconds} seconds)")
    if stats.games:
        print(f"Clean 5v5 min per game       : "
              f"{stats.team_toi_seconds / 60 / len(stats.games):.2f}")
    print(f"{stats.team + ' skaters in table':<29}: {len(rows)}")

    print()
    print("Per-game clean 5v5:")
    for game in stats.games:
        print(f"  {game.label:<34} {game.clean_5v5_seconds / 60:6.2f} min"
              f"   GF {game.gf}  GA {game.ga}   [{game.source}]")

    if skipped:
        print()
        print("SKIPPED GAMES (shift data missing or incomplete):")
        for label, reason in skipped:
            print(f"  {label}")
            print(f"    -> {reason}")

    if diagnostics.missing_from_boxscore:
        print()
        print(f"Skaters with shifts but no boxscore +/- entry: "
              f"{len(diagnostics.missing_from_boxscore)}")
        for entry in diagnostics.missing_from_boxscore[:6]:
            print(f"    {entry}")

    print()
    print("Internal consistency:")
    mark = "OK" if diagnostics.toi_identity_ok else "FAIL"
    print(f"  [{mark}] 5 skaters per clean-5v5 second: {diagnostics.toi_identity_detail}")

    print()
    print("Feed cross-checks (play-by-play situationCode vs shift-chart on-ice state):")
    print(f"  Goals excluded, situationCode not 1551 : {diagnostics.non_1551_goals_excluded}")
    print(f"  Shootout goals excluded                : {diagnostics.shootout_goals_excluded}")
    for label, entries in (
        ("1551 in pbp but not 5v5 in shifts", diagnostics.code_1551_not_clean_in_shifts),
        ("5v5 in shifts but not 1551 in pbp", diagnostics.clean_in_shifts_but_other_code),
        (f"not 5 {stats.team} skaters on ice for a 1551 goal",
         diagnostics.wrong_skater_count_on_ice),
        ("pbp scorer not on ice per shift data", diagnostics.scorer_not_on_ice),
    ):
        print(f"  {label:<39}: {len(entries)}")
        for entry in entries[:8]:
            print(f"      {entry}")
        if len(entries) > 8:
            print(f"      ... and {len(entries) - 8} more")
    print("=" * 78)
