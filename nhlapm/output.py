"""CSV export, console table, and the validation summary."""

from __future__ import annotations

import csv
from pathlib import Path

from .stats import PlayerRow

CSV_COLUMNS = [
    ("player", lambda r: r.name),
    ("player_id", lambda r: r.player_id),
    ("team", lambda r: r.team),
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
    ("adjusted_per_60", lambda r: "" if r.adjusted_per_60 is None else round(r.adjusted_per_60, 3)),
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


CONSOLE_HEADERS = ("PLAYER", "TEAM", "POS", "GP", "TOI_5v5", "GD_on", "OFF_PM", "NONCLEAN",
                   "EXPECTED", "ADJUSTED", "REL", "GD_on/60")
CONSOLE_WIDTHS = (21, 7, 3, 3, 8, 6, 7, 8, 9, 9, 7, 9)


def _console_line(cells: tuple[str, ...]) -> str:
    out = [cells[0].ljust(CONSOLE_WIDTHS[0])[:CONSOLE_WIDTHS[0]],
           cells[1].ljust(CONSOLE_WIDTHS[1]),
           cells[2].center(CONSOLE_WIDTHS[2])]
    out.extend(cell.rjust(width) for cell, width in zip(cells[3:], CONSOLE_WIDTHS[3:]))
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
            row.team,
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
