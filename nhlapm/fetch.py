"""HTTP access to NHL public feeds, with a raw-response disk cache.

Every response is cached verbatim under the cache directory so reruns cost no
API calls. Only successful, parseable responses are cached -- a transport
failure must not be frozen into the cache as if it were data.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

USER_AGENT = "Mozilla/5.0 (compatible; hawks-adjusted-pm/1.1)"
REQUEST_TIMEOUT = 45
RETRIES = 3
THROTTLE_SECONDS = 0.35

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
        self.cache_hits = 0
        self._last_call = 0.0

    # -- plumbing ---------------------------------------------------------

    def _cache_path(self, key: str, ext: str = "json") -> Path:
        return self.cache_dir / f"{key}.{ext}"

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
        path = self._cache_path(key)
        if allow_cache and not self.refresh and path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.cache_hits += 1
                return payload
            except json.JSONDecodeError:
                self._log(f"  cache corrupt, refetching: {path.name}")

        body = self._download(url, validate_json=True)
        path.write_bytes(body)
        return json.loads(body.decode("utf-8"))

    def get_text(self, url: str, key: str, ext: str = "htm") -> str:
        """Fetch a text document, cached verbatim as bytes."""
        path = self._cache_path(key, ext)
        if not self.refresh and path.exists():
            self.cache_hits += 1
            return path.read_bytes().decode(HTML_ENCODING, errors="replace")

        body = self._download(url, validate_json=False)
        path.write_bytes(body)
        return body.decode(HTML_ENCODING, errors="replace")

    # -- endpoints --------------------------------------------------------

    def schedule(self, team: str, season: str) -> dict:
        url = f"{config.BASE_API_WEB}/club-schedule-season/{team}/{season}"
        # The schedule gains completed games as a season runs, so it is always
        # refetched; it is one call.
        return self.get(url, f"schedule_{team}_{season}", allow_cache=False)

    def play_by_play(self, game_id: int) -> dict:
        url = f"{config.BASE_API_WEB}/gamecenter/{game_id}/play-by-play"
        return self.get(url, f"pbp_{game_id}")

    def boxscore(self, game_id: int) -> dict:
        url = f"{config.BASE_API_WEB}/gamecenter/{game_id}/boxscore"
        return self.get(url, f"boxscore_{game_id}")

    def shifts(self, game_id: int) -> dict:
        url = f"{config.BASE_API_STATS}/shiftcharts?cayenneExp=gameId={game_id}"
        return self.get(url, f"shifts_{game_id}")

    def toi_reports(self, game_id: int, season: str) -> tuple[str, str]:
        """(visitor report, home report) HTML for the fallback shift source."""
        number = str(game_id)[-6:]
        away = self.get_text(
            f"{config.BASE_HTML_REPORTS}/{season}/TV{number}.HTM", f"toi_TV_{game_id}")
        home = self.get_text(
            f"{config.BASE_HTML_REPORTS}/{season}/TH{number}.HTM", f"toi_TH_{game_id}")
        return away, home

    # -- cache maintenance ------------------------------------------------

    def shift_cache_is_empty(self, game_id: int) -> bool:
        """True when a cached shift response exists but holds no rows."""
        path = self._cache_path(f"shifts_{game_id}")
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return True
        return not payload.get("data")

    def drop_shift_cache(self, game_id: int) -> None:
        self._cache_path(f"shifts_{game_id}").unlink(missing_ok=True)
