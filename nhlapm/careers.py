"""docs/data/player_seasons.json: one row per player, per season, per club.

Season-range and career views would otherwise need every game-level season
file -- 19 downloads, tens of megabytes -- on a phone. This file is the
pre-folded form: each row is a *stint* (one skater, one season, one club) with
every column as a season total, so any range of seasons, or a whole career, is
a sum of rows.

Columns (all totals over the stint's included games):

    s     index into `seasons`
    pid   player id
    tm    club abbreviation as of that season
    gp, toi (seconds), gf, ga, pm     as in the season file
    xm    Expected in ten-thousandths of a goal (integer; Expected is the only
          non-integer total, and summed over a 19-season career the rounding
          stays under 0.001 -- below any displayed digit)
    toff  off-ice clean-5v5 seconds over the same games
    goff  off-ice clean-5v5 goal differential over the same games
    rk    1 if the NHL classes him a rookie that season

Rel for any multi-stint row -- a traded skater's season, a season range, a
career -- is the sum of each stint's Rel (aggregate.stint_rel), never a ratio
over pooled bench minutes from different clubs.

Conferences and divisions are stored per season (`teams`), because they move:
the 2013 realignment, the 2020-21 temporary divisions (no conferences that
year), relocations (ATL to WPG, ARI to UTA).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import store
from .store import Season

FORMAT_VERSION = 1
STINT_COLS = ["s", "pid", "tm", "gp", "toi", "gf", "ga", "pm", "xm", "toff", "goff", "rk"]
FILE_NAME = "player_seasons.json"


def season_stints(season: Season) -> list[list]:
    """Stint rows for one season, without the season index (prepended later)."""
    acc: dict[tuple[int, str], list] = {}
    for r in season.rows:
        row = acc.setdefault((r.player_id, r.team), [0, 0, 0, 0, 0, 0.0, 0, 0])
        row[0] += 1
        row[1] += r.toi_5v5
        row[2] += r.gf_on
        row[3] += r.ga_on
        row[4] += r.official_pm
        row[5] += r.expected_game
        row[6] += r.toi_off
        row[7] += r.gd_off
    out = []
    for (pid, team), (gp, toi, gf, ga, pm, xp, toff, goff) in sorted(acc.items()):
        out.append([pid, team, gp, toi, gf, ga, pm, round(xp * 10000), toff, goff,
                    int(pid in season.rookies)])
    return out


def season_teams(season: Season) -> dict:
    return {"teams": {abbr: list(v) for abbr, v in sorted(season.teams.items())},
            "confs": dict(season.conferences), "divs": dict(season.divisions)}


def path(docs_dir: Path) -> Path:
    return store.data_dir(docs_dir) / FILE_NAME


def load(docs_dir: Path) -> dict:
    """{season id: {"stints": [...], "teams": {...}, ...}} from the file on disk."""
    p = path(docs_dir)
    if not p.exists():
        return {}
    payload = json.loads(p.read_text(encoding="utf-8"))
    seasons = payload["seasons"]
    out = {sid: {"stints": [], **payload["meta"].get(sid, {})} for sid in seasons}
    for row in payload["rows"]:
        out[seasons[row[0]]]["stints"].append(row[1:])
    return out


def update(docs_dir: Path, season: Season, existing: dict | None = None) -> dict:
    """The file's payload with one season's slice replaced.

    Any season file on disk that the payload lacks is folded in too, so the
    file always covers exactly the published seasons, whichever job wrote last.
    """
    data = load(docs_dir) if existing is None else existing
    data[season.season] = {"stints": season_stints(season), **season_teams(season)}
    for p in store.data_dir(docs_dir).glob("[0-9]" * 8 + ".json"):
        if p.stem not in data:
            other = store.load_season(docs_dir, p.stem)
            data[p.stem] = {"stints": season_stints(other), **season_teams(other)}
    return to_json(data)


def to_json(data: dict) -> dict:
    seasons = sorted(data)
    rows = []
    for i, sid in enumerate(seasons):
        rows.extend([i, *stint] for stint in data[sid]["stints"])
    return {"v": FORMAT_VERSION, "seasons": seasons, "cols": STINT_COLS,
            "meta": {sid: {k: v for k, v in data[sid].items() if k != "stints"}
                     for sid in seasons},
            "rows": rows}


def rebuild_from_seasons(docs_dir: Path) -> dict:
    """Recompute every season's slice from the game-level files (a full check)."""
    data = {}
    for p in sorted(store.data_dir(docs_dir).glob("[0-9]" * 8 + ".json")):
        season = store.load_season(docs_dir, p.stem)
        data[season.season] = {"stints": season_stints(season), **season_teams(season)}
    return to_json(data)
