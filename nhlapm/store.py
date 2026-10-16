"""The committed, processed data: one file per season plus shared lookups.

    docs/data/index.json      manifest: which seasons exist, which is default
    docs/data/players.json    player id -> name, position, shoots, birthdate
    docs/data/20262027.json   one season: teams, games, player-game rows

Season files are compact on purpose -- arrays, not objects, with the column
order declared once in a `cols` list beside each table, so the file documents
itself without repeating keys 50,000 times. Everything stored is an integer
or a short string; Expected and the off-ice components are derived on load
(see records.py), so the file cannot disagree with the definition.

Season-specific facts (conference alignment, rookie status) live in the
season file; facts about a person (name, birthdate) live in players.json. A
past season is therefore one more season file and one more manifest entry,
and a career page is a fold over the season files keyed by player id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .records import GameRecord, PlayerGame

FORMAT_VERSION = 1

GAME_COLS = ["id", "date", "away", "home", "sec", "agf", "hgf", "src"]
ROW_COLS = ["pid", "g", "h", "toi", "gf", "ga", "pm"]
TEAM_COLS = ["abbr", "name", "conf", "div"]
EXCLUDED_COLS = ["id", "date", "away", "home", "reason"]
PLAYER_COLS = ["name", "pos", "shoots", "dob"]

SOURCES = [config.SOURCE_SHIFTCHARTS, config.SOURCE_HTML_TOI]


@dataclass(frozen=True)
class GameEntry:
    game_id: int
    date: str
    away: str
    home: str
    clean_seconds: int
    away_gf: int
    home_gf: int
    source: str


@dataclass(frozen=True)
class Excluded:
    game_id: int
    date: str
    away: str
    home: str
    reason: str


@dataclass
class Season:
    season: str
    updated: str = ""
    teams: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # abbr -> (name, conf, div)
    conferences: dict[str, str] = field(default_factory=dict)
    divisions: dict[str, str] = field(default_factory=dict)
    games: dict[int, GameEntry] = field(default_factory=dict)
    rows: list[PlayerGame] = field(default_factory=list)
    rookies: set[int] = field(default_factory=set)
    excluded: list[Excluded] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def add_record(self, record: GameRecord) -> None:
        self.games[record.game_id] = GameEntry(
            record.game_id, record.date, record.away, record.home,
            record.clean_seconds, record.away_gf, record.home_gf, record.source)
        self.rows.extend(record.rows)

    def rows_for_game(self, game_id: int) -> list[PlayerGame]:
        return [r for r in self.rows if r.game_id == game_id]


# -- season file -----------------------------------------------------------

def season_to_json(season: Season) -> dict:
    team_list = sorted(season.teams) or sorted({g.home for g in season.games.values()}
                                               | {g.away for g in season.games.values()})
    team_index = {abbr: i for i, abbr in enumerate(team_list)}

    ordered_games = sorted(season.games.values(), key=lambda g: (g.date, g.game_id))
    game_index = {g.game_id: i for i, g in enumerate(ordered_games)}

    rows = sorted(season.rows, key=lambda r: (game_index[r.game_id], r.is_home, r.player_id))
    return {
        "v": FORMAT_VERSION,
        "season": season.season,
        "updated": season.updated,
        "confs": season.conferences,
        "divs": season.divisions,
        "teams": {"cols": TEAM_COLS,
                  "rows": [[abbr, *season.teams.get(abbr, (abbr, "", ""))]
                           for abbr in team_list]},
        "games": {"cols": GAME_COLS,
                  "rows": [[g.game_id, g.date, team_index[g.away], team_index[g.home],
                            g.clean_seconds, g.away_gf, g.home_gf,
                            SOURCES.index(g.source) if g.source in SOURCES else -1]
                           for g in ordered_games]},
        "rows": {"cols": ROW_COLS,
                 "rows": [[r.player_id, game_index[r.game_id], int(r.is_home),
                           r.toi_5v5, r.gf_on, r.ga_on, r.official_pm] for r in rows]},
        "sources": SOURCES,
        "rookies": sorted(season.rookies),
        "excluded": {"cols": EXCLUDED_COLS,
                     "rows": [[e.game_id, e.date, e.away, e.home, e.reason]
                              for e in sorted(season.excluded, key=lambda e: e.game_id)]},
        "checks": season.checks,
    }


def season_from_json(payload: dict) -> Season:
    if payload.get("v") != FORMAT_VERSION:
        raise ValueError(f"unsupported season file version {payload.get('v')!r}")
    season = Season(season=payload["season"], updated=payload.get("updated", ""))
    season.conferences = dict(payload.get("confs", {}))
    season.divisions = dict(payload.get("divs", {}))
    team_list = []
    for abbr, name, conf, div in payload["teams"]["rows"]:
        team_list.append(abbr)
        season.teams[abbr] = (name, conf, div)
    sources = payload.get("sources", SOURCES)

    ordered = []
    for gid, date, away, home, sec, agf, hgf, src in payload["games"]["rows"]:
        entry = GameEntry(gid, date, team_list[away], team_list[home], sec, agf, hgf,
                          sources[src] if 0 <= src < len(sources) else "?")
        season.games[gid] = entry
        ordered.append(entry)

    for pid, g, h, toi, gf, ga, pm in payload["rows"]["rows"]:
        game = ordered[g]
        is_home = bool(h)
        team, opp = (game.home, game.away) if is_home else (game.away, game.home)
        team_gf, team_ga = ((game.home_gf, game.away_gf) if is_home
                            else (game.away_gf, game.home_gf))
        season.rows.append(PlayerGame(
            player_id=pid, game_id=game.game_id, date=game.date, team=team,
            opponent=opp, is_home=is_home, toi_5v5=toi, gf_on=gf, ga_on=ga,
            official_pm=pm, team_toi_5v5=game.clean_seconds,
            team_gf=team_gf, team_ga=team_ga))
    season.rookies = set(payload.get("rookies", []))
    season.excluded = [Excluded(*row) for row in payload.get("excluded", {}).get("rows", [])]
    season.checks = dict(payload.get("checks", {}))
    return season


def dumps_compact(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def data_dir(docs_dir: Path) -> Path:
    return Path(docs_dir) / config.DATA_DIR


def season_path(docs_dir: Path, season: str) -> Path:
    return data_dir(docs_dir) / f"{season}.json"


def default_season(docs_dir: Path) -> str | None:
    """The season the published site opens on, read offline from the manifest."""
    path = data_dir(docs_dir) / "index.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("default")
    except (OSError, json.JSONDecodeError):
        return None


def load_season(docs_dir: Path, season: str) -> Season | None:
    path = season_path(docs_dir, season)
    if not path.exists():
        return None
    return season_from_json(json.loads(path.read_text(encoding="utf-8")))


# -- players and manifest --------------------------------------------------

def load_players(docs_dir: Path) -> dict[int, list]:
    path = data_dir(docs_dir) / "players.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {int(pid): list(values) for pid, values in payload.get("players", {}).items()}


def players_to_json(players: dict[int, list]) -> dict:
    return {"v": FORMAT_VERSION, "cols": PLAYER_COLS,
            "players": {str(pid): players[pid] for pid in sorted(players)}}


def season_label(season: str) -> str:
    return f"{season[:4]}-{season[6:]}" if len(season) == 8 else season


def manifest_to_json(docs_dir: Path, current: Season | None = None) -> dict:
    """Every season file on disk, newest first; the newest is the default.

    The newest season on disk is the current one: the daily run only ever
    writes the current season, and past seasons are older by definition.
    """
    seasons = []
    for path in sorted(data_dir(docs_dir).glob("[0-9]" * 8 + ".json"), reverse=True):
        sid = path.stem
        updated = current.updated if current and sid == current.season else ""
        if not updated:
            try:
                updated = json.loads(path.read_text(encoding="utf-8")).get("updated", "")
            except (OSError, json.JSONDecodeError):
                updated = ""
        seasons.append({"id": sid, "label": season_label(sid), "file": path.name,
                        "updated": updated})
    return {"v": FORMAT_VERSION, "default": seasons[0]["id"] if seasons else "",
            "players": "players.json", "careers": "player_seasons.json",
            "seasons": seasons}
