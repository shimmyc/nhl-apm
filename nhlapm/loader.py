"""One place that turns a scheduled game into a fully populated GameContext.

Both the stats run and the verifier go through here, so they always agree on
which shift source a game used. Preferred source is the JSON shiftcharts feed;
when it returns nothing, the HTML Time On Ice reports are parsed instead.

`html_context` gives the pipeline the same HTML source on demand, for a game
whose shiftcharts rows exist but fail verification against the boxscore (the
JSON feed sometimes carries phantom extra shifts; the HTML report for the same
game has matched the boxscore to the second in every such case seen).
"""

from __future__ import annotations

from . import config, html_toi, parse
from .fetch import Fetcher, FetchError
import copy

from .parse import GameContext, GameRef


def load_game_context(fetcher: Fetcher, ref: GameRef, season: str,
                      html_fallback: bool = True) -> GameContext:
    """Raises FetchError / KeyError / ValueError on unusable payloads."""
    pbp = fetcher.play_by_play(ref.game_id)
    shift_payload = fetcher.shifts(ref.game_id)
    context = parse.build_game_context(ref, pbp, shift_payload)

    if context.shifts or not html_fallback:
        return context

    context.shift_warnings.append(
        f"shiftcharts returned {len(shift_payload.get('data', []))} rows; "
        f"falling back to the HTML TOI reports")
    try:
        away_html, home_html = fetcher.toi_reports(ref.game_id, season)
    except FetchError as exc:
        context.shift_warnings.append(f"HTML TOI reports unavailable: {exc}")
        return context

    shifts, warnings = html_toi.shifts_from_reports(away_html, home_html, context)
    if shifts:
        context.replace_shifts(shifts, config.SOURCE_HTML_TOI, warnings)
    else:
        context.shift_warnings.extend(warnings)
    return context


def html_context(fetcher: Fetcher, context: GameContext, season: str
                 ) -> tuple[GameContext | None, list[str]]:
    """A copy of `context` with its shifts taken from the HTML TOI reports."""
    try:
        away_html, home_html = fetcher.toi_reports(context.ref.game_id, season)
    except FetchError as exc:
        return None, [f"HTML TOI reports unavailable: {exc}"]
    alt = copy.copy(context)
    alt.shift_warnings = []
    shifts, warnings = html_toi.shifts_from_reports(away_html, home_html, alt)
    if not shifts:
        return None, warnings
    alt.replace_shifts(shifts, config.SOURCE_HTML_TOI, warnings)
    return alt, warnings
