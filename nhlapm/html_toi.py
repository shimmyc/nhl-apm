"""Fallback shift source: the NHL HTML Time On Ice reports.

Used when `shiftcharts` returns zero rows for a game. The reports are one file
per team -- "TV" for the visitor, "TH" for the home side -- and each lists every
player who took a shift, goalies included, with per-shift start and end times.

Output is the same `Shift` record the timeline module already consumes, so
nothing downstream knows or cares which feed a game came from.

Report layout, per player section:

    <td class="playerHeading">88 KANE, PATRICK</td>
    Shift # | Per | Start of Shift     | End of Shift       | Duration | Event
    1       | 1   | 0:35 / 19:25       | 1:17 / 18:43       | 00:42    |
    ...
    TOT     | 21  | 00:47 | 17:30 | ...        <- per-period summary, not a shift

Times are "elapsed / remaining" within the period; only elapsed is used. The
summary rows are excluded structurally: a shift row's first cell is the shift
number and its time cells contain "/", which is true of no summary row.
"""

from __future__ import annotations

import re
import unicodedata

from . import config
from .parse import GameContext, Player, Shift, clock_to_seconds

PLAYER_HEADING_RE = re.compile(
    r'class="playerHeading[^"]*"[^>]*>(.*?)</td>', re.S | re.I)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")
HEADING_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
ELAPSED_RE = re.compile(r"^\s*(\d+:\d{2})\s*/\s*\d+:\d{2}\s*$")


def _text(fragment: str) -> str:
    """Strip tags and normalise whitespace/entities in one cell."""
    plain = TAG_RE.sub("", fragment)
    plain = plain.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(plain.split())


def _normalise_name(value: str) -> str:
    """Casefold and strip accents so 'TERAVAINEN' matches 'Teravainen'."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^A-Z ]", "", stripped.upper().replace("-", " ")).strip()


def _period_number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    return config.HTML_PERIOD_ALIASES.get(value.upper())


def _elapsed(cell: str) -> str | None:
    """'0:35 / 19:25' -> '0:35'.

    The "elapsed / remaining" pair is required, not merely preferred. Each
    player's section ends with a per-period summary table whose rows also lead
    with a digit and also hold MM:SS cells -- ['1', '8', '00:45', '06:00', ...].
    Demanding the slash is what separates a shift from that summary; accepting a
    bare MM:SS silently adds one phantom shift per period.
    """
    match = ELAPSED_RE.match(cell)
    return match.group(1) if match else None


def _player_sections(html: str) -> list[tuple[int, str, str]]:
    """[(sweater, name-as-printed, section html)] for each player in the report."""
    headings = [(m.start(), _text(m.group(1))) for m in PLAYER_HEADING_RE.finditer(html)]
    sections: list[tuple[int, str, str]] = []
    for index, (position, label) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(html)
        match = HEADING_RE.match(label)
        if not match:
            continue
        sections.append((int(match.group(1)), match.group(2), html[position:end]))
    return sections


def _section_shifts(section: str) -> list[tuple[int, int, int, str | None]]:
    """[(period, start_second_in_period, end_second_in_period, duration)]."""
    out: list[tuple[int, int, int, str | None]] = []
    for row in ROW_RE.findall(section):
        cells = [_text(cell) for cell in CELL_RE.findall(row)]
        if len(cells) < 5 or not cells[0].isdigit():
            continue
        start = _elapsed(cells[2])
        end = _elapsed(cells[3])
        if start is None or end is None:
            continue  # summary row, or a cell without the "elapsed / remaining" shape
        period = _period_number(cells[1])
        if period is None:
            continue
        out.append((period, clock_to_seconds(start), clock_to_seconds(end),
                    cells[4] if len(cells) > 4 else None))
    return out


def _team_index(context: GameContext, team_id: int) -> dict[int, Player]:
    return {
        player.sweater: player
        for player in context.roster.values()
        if player.team_id == team_id and player.sweater is not None
    }


def _parse_one_team(html: str, context: GameContext, team_id: int, label: str,
                    shifts: list[Shift], warnings: list[str]) -> int:
    by_sweater = _team_index(context, team_id)
    matched = 0
    unmatched: list[str] = []
    name_mismatch: list[str] = []
    bad_rows = 0

    for sweater, printed_name, section in _player_sections(html):
        player = by_sweater.get(sweater)
        if player is None:
            unmatched.append(f"#{sweater} {printed_name}")
            continue

        # The sweater number is the join key; the surname is the cross-check.
        # Compare against the roster's own lastName field -- splitting a flat
        # "Ethan Del Mastro" on whitespace would report a phantom mismatch.
        printed_surname = _normalise_name(printed_name.split(",")[0])
        roster_surname = _normalise_name(player.last_name or player.name.split()[-1])
        if printed_surname and roster_surname and printed_surname != roster_surname:
            name_mismatch.append(
                f"#{sweater} report says {printed_surname}, roster says {roster_surname}")

        rows = _section_shifts(section)
        if not rows:
            continue
        matched += 1
        for period, start, end, _duration in rows:
            offset = (period - 1) * config.PERIOD_BLOCK_SECONDS
            if end <= start:
                bad_rows += 1
                continue
            shifts.append(Shift(
                player_id=player.player_id,
                team_id=player.team_id,
                team_abbrev=player.team_abbrev,
                period=period,
                start=offset + start,
                end=offset + end,
            ))

    if unmatched:
        warnings.append(
            f"{label} report: {len(unmatched)} player(s) not matched to the pbp "
            f"roster by sweater number ({', '.join(unmatched[:4])})")
    if name_mismatch:
        warnings.append(
            f"{label} report: {len(name_mismatch)} sweater/surname mismatch "
            f"({'; '.join(name_mismatch[:3])})")
    if bad_rows:
        warnings.append(f"{label} report: {bad_rows} row(s) with unusable times dropped")
    return matched


def shifts_from_reports(away_html: str, home_html: str, context: GameContext
                        ) -> tuple[list[Shift], list[str]]:
    """Parse both team reports into Shift records.

    Players are joined to the play-by-play roster on (team, sweater number),
    which is how the reports identify them -- they carry no player ids.
    """
    shifts: list[Shift] = []
    warnings: list[str] = []

    away_count = _parse_one_team(away_html, context, context.away_id,
                                 context.away_abbrev, shifts, warnings)
    home_count = _parse_one_team(home_html, context, context.home_id,
                                 context.home_abbrev, shifts, warnings)

    if not shifts:
        warnings.append("HTML TOI reports produced no shifts")
    else:
        warnings.insert(0, f"recovered {len(shifts)} shifts from HTML TOI reports "
                           f"({context.away_abbrev} {away_count} players, "
                           f"{context.home_abbrev} {home_count})")
    return shifts, warnings
