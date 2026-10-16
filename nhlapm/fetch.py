"""HTTP access to NHL public feeds, with a raw-response disk cache.

Every response is cached verbatim under the cache directory so reruns cost no
API calls. Only successful, parseable responses are cached -- a transport
failure must not be frozen into the cache as if it were data.

New entries are written gzipped (`key.json.gz`, `key.htm.gz`): a game's raw
payloads shrink roughly tenfold, which is what makes a 19-season local cache
practical. Plain entries written before that are still read, so an existing
cache stays valid; a cache file is never rewritten just to compress it.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

USER_AGENT = "Mozilla/5.0 (compatible; nhl-adjusted-pm/2.0)"
REQUEST_TIMEOUT = 45
RETRIES = 3
# About one request a second. A full-season backfill is ~4,000 requests, so
# this is roughly an hour; politeness to a free, keyless API is worth it.
THROTTLE_SECONDS = 1.0

# The HTML game reports are served as windows-1252, not UTF-8.
HTML_ENCODING = "cp1252"


class FetchError(RuntimeError):
    """A response could not be retrieved or parsed."""


class Fetcher:
    """Fetches JSON and HTML with a verbatim on-disk cache."""

    def __init__(self, cache_dir: Path, refresh: bool = False, verbose: bool = True):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.refresh = refresh
        self.verbose = verbose
        self.api_calls = 0
        self.game_calls = 0     # network requests for per-game payloads only
        self.cache_hits = 0
        self._last_call = 0.0

    # -- plumbing ---------------------------------------------------------

    def _cache_path(self, key: str, ext: str = "json") -> Path:
        """Where an entry is written: always the gzipped name."""
        return self.cache_dir / f"{key}.{ext}.gz"

    def _cached(self, key: str, ext: str = "json") -> Path | None:
        """The existing entry for a key, gzipped or legacy plain, else None."""
        for path in (self.cache_dir / f"{key}.{ext}.gz", self.cache_dir / f"{key}.{ext}"):
            if path.exists():
                return path
        return None

    @staticmethod
    def _read(path: Path) -> bytes:
        data = path.read_bytes()
        return gzip.decompress(data) if path.suffix == ".gz" else data

    def _write(self, key: str, ext: str, body: bytes) -> None:
        self._cache_path(key, ext).write_bytes(gzip.compress(body, compresslevel=6))
        legacy = self.cache_dir / f"{key}.{ext}"
        legacy.unlink(missing_ok=True)   # never leave two disagreeing copies

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < THROTTLE_SECONDS:
            time.sleep(THROTTLE_SECONDS - elapsed)
        self._last_call = time.monotonic()

    def _download(self, url: str, validate_json: bool) -> bytes:
        """Raw body bytes. Raises FetchError after exhausting retries."""
        last_error: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            self._throttle()
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    body = response.read()
                if validate_json:
                    # Parse before caching so a truncated body is retried, not stored.
                    json.loads(body.decode("utf-8"))
                elif not body.strip():
                    raise ValueError("empty response body")
                self.api_calls += 1
                return body
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                    json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
                last_error = exc
                if attempt < RETRIES:
                    time.sleep(1.5 * attempt)
        raise FetchError(f"{url}: {last_error}")

    def get(self, url: str, key: str, allow_cache: bool = True) -> dict:
        """Fetch JSON."""
        path = self._cached(key)
        if allow_cache and not self.refresh and path is not None:
            try:
                payload = json.loads(self._read(path).decode("utf-8"))
                self.cache_hits += 1
                return payload
            except (json.JSONDecodeError, OSError, EOFError, UnicodeDecodeError):
                self._log(f"  cache corrupt, refetching: {path.name}")

        body = self._download(url, validate_json=True)
        self._write(key, "json", body)
        return json.loads(body.decode("utf-8"))

    def get_text(self, url: str, key: str, ext: str = "htm") -> str:
        """Fetch a text document, cached verbatim as bytes."""
        path = self._cached(key, ext)
        if not self.refresh and path is not None:
            try:
                body = self._read(path)
                self.cache_hits += 1
                return body.decode(HTML_ENCODING, errors="replace")
            except (OSError, EOFError):
                self._log(f"  cache corrupt, refetching: {path.name}")

        body = self._download(url, validate_json=False)
        self._write(key, ext, body)
        return body.decode(HTML_ENCODING, errors="replace")

    # -- endpoints --------------------------------------------------------

    def schedule(self, team: str, season: str) -> dict:
        url = f"{config.BASE_API_WEB}/club-schedule-season/{team}/{season}"
        # The schedule gains completed games as a season runs, so it is always
        # refetched; it is one call.
        return self.get(url, f"schedule_{team}_{season}", allow_cache=False)

    def _game_get(self, url: str, key: str) -> dict:
        before = self.api_calls
        try:
            return self.get(url, key)
        finally:
            self.game_calls += self.api_calls - before

    def play_by_play(self, game_id: int) -> dict:
        url = f"{config.BASE_API_WEB}/gamecenter/{game_id}/play-by-play"
        return self._game_get(url, f"pbp_{game_id}")

    def boxscore(self, game_id: int) -> dict:
        url = f"{config.BASE_API_WEB}/gamecenter/{game_id}/boxscore"
        return self._game_get(url, f"boxscore_{game_id}")

    def shifts(self, game_id: int) -> dict:
        url = f"{config.BASE_API_STATS}/shiftcharts?cayenneExp=gameId={game_id}"
        return self._game_get(url, f"shifts_{game_id}")

    def toi_reports(self, game_id: int, season: str) -> tuple[str, str]:
        """(visitor report, home report) HTML for the fallback shift source."""
        number = str(game_id)[-6:]
        before = self.api_calls
        away = self.get_text(
            f"{config.BASE_HTML_REPORTS}/{season}/TV{number}.HTM", f"toi_TV_{game_id}")
        home = self.get_text(
            f"{config.BASE_HTML_REPORTS}/{season}/TH{number}.HTM", f"toi_TH_{game_id}")
        self.game_calls += self.api_calls - before
        return away, home

    def schedule_week(self, date: str) -> dict:
        """The league-wide schedule for the week starting at `date` (YYYY-MM-DD).

        A cached week is reused only when every game in it has gone final;
        a week that still holds an unplayed or live game is refetched.
        """
        url = f"{config.BASE_API_WEB}/schedule/{date}"
        key = f"schedule_week_{date}"
        payload = self.get(url, key)
        if not _week_settled(payload):
            payload = self.get(url, key, allow_cache=False)
        return payload

    def seasons(self) -> dict:
        """Every NHL season: id, regular-season start/end, scheduled games."""
        return self.get(f"{config.BASE_API_STATS}/season", "seasons", allow_cache=False)

    def standings(self) -> dict:
        """Current standings: team -> conference and division."""
        return self.get(f"{config.BASE_API_WEB}/standings/now",
                        "standings_now", allow_cache=False)

    def standings_on(self, date: str) -> dict:
        """Standings as of a past date: that season's clubs, conferences, divisions.

        Cached: standings for a date that has passed do not change.
        """
        return self.get(f"{config.BASE_API_WEB}/standings/{date}", f"standings_{date}")

    # The season-level reports below change while a season runs, so the
    # current season refetches them (fresh=True); a finished season's are
    # cached like any other raw response.

    def skater_bios(self, season: str, fresh: bool = True) -> dict:
        """Every skater who has played this season: position, shoots, birthdate."""
        url = (f"{config.BASE_API_STATS}/skater/bios?limit=-1&cayenneExp="
               f"seasonId={season}%20and%20gameTypeId={config.GAME_TYPE_REGULAR}")
        return self.get(url, f"skater_bios_{season}", allow_cache=not fresh)

    def rookies(self, season: str, fresh: bool = True) -> dict:
        """Skaters the NHL classifies as rookies this season (its own definition)."""
        url = (f"{config.BASE_API_STATS}/skater/summary?limit=-1&cayenneExp="
               f"seasonId={season}%20and%20gameTypeId={config.GAME_TYPE_REGULAR}"
               f"%20and%20isRookie=%221%22")
        return self.get(url, f"rookies_{season}", allow_cache=not fresh)

    def skater_season_summary(self, season: str, fresh: bool = True) -> dict:
        """The NHL's season totals per skater (one row per player, all clubs
        combined) -- the independent source for the season +/- check."""
        url = (f"{config.BASE_API_STATS}/skater/summary?limit=-1&cayenneExp="
               f"seasonId={season}%20and%20gameTypeId={config.GAME_TYPE_REGULAR}")
        return self.get(url, f"skater_summary_{season}", allow_cache=not fresh)

    def game_payloads_cached(self, game_id: int) -> bool:
        return all(self._cached(f"{kind}_{game_id}") is not None
                   for kind in ("pbp", "shifts", "boxscore"))

    def drop_game_cache(self, game_id: int) -> None:
        """Forget every cached payload for one game so it is refetched fresh."""
        for key in (f"pbp_{game_id}", f"shifts_{game_id}", f"boxscore_{game_id}"):
            self._drop(key, "json")
        for side in ("TV", "TH"):
            self._drop(f"toi_{side}_{game_id}", "htm")

    def _drop(self, key: str, ext: str) -> None:
        while (path := self._cached(key, ext)) is not None:
            path.unlink()

    # -- cache maintenance ------------------------------------------------

    def shift_cache_is_empty(self, game_id: int) -> bool:
        """True when a cached shift response exists but holds no rows."""
        path = self._cached(f"shifts_{game_id}")
        if path is None:
            return False
        try:
            payload = json.loads(self._read(path).decode("utf-8"))
        except (json.JSONDecodeError, OSError, EOFError):
            return True
        return not payload.get("data")

    def drop_shift_cache(self, game_id: int) -> None:
        self._drop(f"shifts_{game_id}", "json")


def _week_settled(payload: dict) -> bool:
    """True when every regular-season game in a weekly schedule has gone final."""
    for day in payload.get("gameWeek", []):
        for game in day.get("games", []):
            if game.get("gameType") != config.GAME_TYPE_REGULAR:
                continue
            if game.get("gameState") not in config.COMPLETED_GAME_STATES:
                return False
    return True
