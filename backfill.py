"""Historical backfill: past regular seasons, one season file each.

    python backfill.py --from 20072008 --to 20252026 --push
    python backfill.py --status                    # progress, from the log
    python backfill.py --only 20122013             # one season
    python backfill.py --publish-held 20122013 --push   # after reviewing a held season

Standalone and restartable: run it, stop it (Ctrl+C, a reboot, a dropped
connection), run the same command again. Every raw response is cached
gzipped under cache/history/<season>/, so a restart re-reads finished games
from disk at no network cost and only reaches the API for what it never
fetched. A season whose file is already published is skipped unless
--redo is given.

Per season, exactly the rules the daily job uses:

* every game is computed and verified on its own; a failing game is
  quarantined and listed, not fatal;
* the season file is serialised, read back, and the season checks run on
  what was read back, with games per team compared to that season's final
  standings;
* plus one check only a finished season allows: each skater's season
  Official_PM must equal the NHL's own season +/- (stats skater/summary).

A season that fails a season-level check is NOT published and NOT silently
dropped: its file goes to backfill_held/<season>.json and the reason goes to
the log and to backfill_report.json, for a person to look at. If, having
looked, you want it on the site anyway, --publish-held <season> publishes
that file exactly as held (its quarantined games stay listed on the page).

Requests are throttled to about one a second (fetch.THROTTLE_SECONDS).
Progress: backfill.log (one line per game and per season) and
backfill_progress.json (machine-readable, rewritten every game).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from nhlapm import careers, checks, config, pipeline, seasons, store
from nhlapm.fetch import Fetcher, FetchError

ROOT = Path(__file__).resolve().parent
FIRST_SEASON = "20072008"     # the first season with HTML shift reports for every game
LOG = ROOT / "backfill.log"
PROGRESS = ROOT / "backfill_progress.json"
REPORT = ROOT / "backfill_report.json"
HELD = ROOT / "backfill_held"
LOCK = ROOT / "backfill.lock"
MAX_PM_MISMATCH_SHARE = 0.02    # more skaters than this disagreeing with the NHL holds a season


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Log:
    def __init__(self, path: Path, echo: bool = True):
        self.path, self.echo = path, echo

    def __call__(self, message: str) -> None:
        line = f"{now()} {message}"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        if self.echo:
            print(line, flush=True)


def season_ids(first: str, last: str, fetcher: Fetcher) -> list[str]:
    table = seasons.all_seasons(fetcher)
    return sorted(s for s in table if first <= s <= last)


def write_json(path: Path, payload) -> None:
    # Windows can refuse the rename for a moment while an indexer or a reader
    # has the target open; a progress file is not worth dying over.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))
    path.write_text(tmp.read_text(encoding="utf-8"), encoding="utf-8")


def load_report() -> dict:
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seasons": {}}


def quarantined_plus_minus(fetcher: Fetcher, season: store.Season) -> dict[int, int]:
    """Boxscore +/- per skater summed over the season's quarantined games."""
    out: dict[int, int] = {}
    for item in season.excluded:
        try:
            box = fetcher.boxscore(item.game_id)
        except FetchError:
            continue
        for side in ("homeTeam", "awayTeam"):
            groups = box.get("playerByGameStats", {}).get(side, {})
            for group in ("forwards", "defense"):
                for entry in groups.get(group, []):
                    if entry.get("plusMinus") is not None:
                        out[entry["playerId"]] = out.get(entry["playerId"], 0) + entry["plusMinus"]
    return out


# -- publishing ------------------------------------------------------------

def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


SHARED = ["docs/data/players.json", "docs/data/index.json", "docs/data/player_seasons.json"]


def write_shared(docs_dir: Path, shipped: store.Season, players_new: dict[int, list]) -> None:
    """Merge one season into the files every season shares, as they are on disk now."""
    data_dir = store.data_dir(docs_dir)
    players = store.load_players(docs_dir)
    for pid, meta in players_new.items():
        players.setdefault(pid, meta)   # never overwrite a newer bio with an older one
    (data_dir / "players.json").write_text(
        store.dumps_compact(store.players_to_json(players)), encoding="utf-8")
    (data_dir / careers.FILE_NAME).write_text(
        store.dumps_compact(careers.update(docs_dir, shipped)), encoding="utf-8")
    (data_dir / "index.json").write_text(
        store.dumps_compact(store.manifest_to_json(docs_dir)), encoding="utf-8")


def publish(docs_dir: Path, shipped: store.Season, season_text: str,
            players_new: dict[int, list], push: bool, log: Log) -> None:
    season_file = store.season_path(docs_dir, shipped.season)
    rel = ([str(season_file.relative_to(ROOT)).replace("\\", "/")] + SHARED) if push else []
    for attempt in range(1, 4):
        if push:
            pulled = git("pull", "--rebase", "--autostash", "-q", "origin", "main")
            if pulled.returncode != 0:
                log(f"  git pull failed: {pulled.stderr.strip()[:300]}")
        season_file.write_text(season_text, encoding="utf-8")
        write_shared(docs_dir, shipped, players_new)
        if not push:
            return
        git("add", *rel)
        if git("diff", "--cached", "--quiet").returncode == 0:
            log("  nothing new to commit")
            return
        message = (f"Backfill {store.season_label(shipped.season)}: "
                   f"{len(shipped.games)} games, {len(shipped.excluded)} quarantined\n\n"
                   "Processed data only; the raw cache stays local.\n")
        committed = git("commit", "-q", "-m", message)
        if committed.returncode != 0:
            log(f"  git commit failed: {committed.stderr.strip()[:300]}")
            return
        pushed = git("push", "-q", "origin", "HEAD:main")
        if pushed.returncode == 0:
            log(f"  pushed {git('rev-parse', '--short', 'HEAD').stdout.strip()}")
            return
        # Someone (the daily job) pushed first. Undo our commit, put the
        # shared files back to what is committed, and merge again on top of
        # theirs: the season's results are in memory, so nothing is lost.
        log(f"  push rejected (attempt {attempt}); re-merging on top of origin")
        git("reset", "--soft", "HEAD~1")
        git("restore", "--staged", *rel)
        git("checkout", "--", *SHARED)
    log("  could not push after 3 attempts; files are written locally, push them by hand")


# -- one season ------------------------------------------------------------

def run_season(sid: str, args, log: Log, report: dict) -> str:
    docs_dir = Path(args.docs_dir)
    fetcher = Fetcher(Path(args.cache_root) / sid, verbose=False)
    info = seasons.season_info(fetcher, sid)
    label = store.season_label(sid)
    started = time.monotonic()

    log(f"== {label}: regular season {info.start} to {info.end}, "
        f"{info.total_games} games scheduled ({info.games_per_team} per team)")

    progress = {"season": sid, "done": 0, "total": 0, "quarantined": 0, "started": now()}

    def per_game(message: str) -> None:
        # pipeline.run_league logs "  [  12/1230] ..." per game.
        if message.lstrip().startswith("["):
            progress["done"] += 1
            if "QUARANTINED" in message:
                progress["quarantined"] += 1
                log(f"  {sid} {message.strip()}")
            elif progress["done"] % 50 == 0:
                log(f"  {sid} {progress['done']}/{progress['total']} done, "
                    f"{progress['quarantined']} quarantined, {fetcher.api_calls} requests")
            progress["requests"] = fetcher.api_calls
            write_json(PROGRESS, progress)
        elif message.lstrip().startswith("schedule:"):
            progress["total"] = int(message.split()[1])
            log(f"  {sid} {message.strip()}")
            write_json(PROGRESS, progress)

    run = pipeline.run_league(fetcher, sid, None, rebuild=True, historical=True,
                              refetch_quarantined=False, log=per_game)
    season = run.season
    season.updated = now().replace(" ", "T")
    players, missing = pipeline.collect_player_metadata(fetcher, sid, season, {}, fresh=False)
    if missing:
        pipeline.fill_from_rosters(fetcher, season, players, missing)
    season.checks = {"games_included": len(season.games),
                     "games_quarantined": len(season.excluded),
                     "diagnostics": {k: sorted(set(v)) for k, v in run.diagnostics.items()}}

    season_text = store.dumps_compact(store.season_to_json(season))
    shipped = store.season_from_json(json.loads(season_text))
    verdict = checks.verify_season(shipped, run.standings_games, historical=True)
    summary = fetcher.skater_season_summary(sid, fresh=False)
    pm = checks.season_plus_minus(shipped, summary, quarantined_plus_minus(fetcher, shipped),
                                  {pid: m[0] for pid, m in players.items()})

    blocking = list(verdict.blocking)
    if pm.share > MAX_PM_MISMATCH_SHARE:
        blocking.append(f"season +/- disagrees with the NHL for {len(pm.mismatches)} of "
                        f"{pm.players_checked} skaters ({pm.share:.1%})")
    for line in verdict.lines:
        log(f"  {sid} check: {line}")
    log(f"  {sid} check: season +/- vs NHL skater summary: "
        f"{pm.players_checked - len(pm.mismatches)}/{pm.players_checked} skaters agree")
    for line in pm.mismatches[:10]:
        log(f"  {sid}   +/- mismatch: {line}")
    for key, entries in sorted(season.checks["diagnostics"].items()):
        log(f"  {sid} diagnostic (non-blocking) {key}: {len(entries)}")
    sources = {}
    for g in shipped.games.values():
        sources[g.source] = sources.get(g.source, 0) + 1

    entry = {
        "label": label, "finished": now(),
        "games_scheduled": info.total_games,
        "games_included": len(shipped.games), "games_quarantined": len(shipped.excluded),
        "sources": sources,
        "quarantined": [f"{e.game_id} {e.date} {e.away}@{e.home}: {e.reason}"
                        for e in shipped.excluded],
        "pm_players_checked": pm.players_checked, "pm_mismatches": pm.mismatches,
        "season_checks": verdict.lines, "blocking": blocking,
        "diagnostics": {k: len(v) for k, v in season.checks["diagnostics"].items()},
        "requests": fetcher.api_calls,
        "minutes": round((time.monotonic() - started) / 60, 1),
    }
    if blocking:
        HELD.mkdir(exist_ok=True)
        (HELD / f"{sid}.json").write_text(season_text, encoding="utf-8")
        for line in blocking:
            log(f"  {sid} HELD: {line}")
        log(f"  {sid} not published; file kept at backfill_held/{sid}.json for review")
        entry["status"] = "held"
    else:
        publish(docs_dir, shipped, season_text, players, args.push, log)
        entry["status"] = "published"
        log(f"  {sid} published: {len(shipped.games)} games, {len(shipped.excluded)} quarantined, "
            f"{fetcher.api_calls} requests, {entry['minutes']} min")
    report["seasons"][sid] = entry
    write_json(REPORT, report)
    return entry["status"]


def publish_held(sid: str, args) -> int:
    log = Log(LOG)
    held = HELD / f"{sid}.json"
    if not held.exists():
        print(f"no held file for {sid}")
        return 1
    text = held.read_text(encoding="utf-8")
    shipped = store.season_from_json(json.loads(text))
    meta = Fetcher(Path(args.cache_root) / sid, verbose=False)
    players, missing = pipeline.collect_player_metadata(meta, sid, shipped, {}, fresh=False)
    if missing:
        pipeline.fill_from_rosters(meta, shipped, players, missing)
    log(f"== {store.season_label(sid)}: publishing the held file after review")
    publish(Path(args.docs_dir), shipped, text, players, args.push, log)
    report = load_report()
    if sid in report["seasons"]:
        report["seasons"][sid]["status"] = "published after review"
        write_json(REPORT, report)
    held.unlink()
    return 0


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def take_lock() -> bool:
    """One backfill at a time: two would double the request rate and race pushes."""
    import os
    try:
        pid = int(LOCK.read_text().strip())
        if pid != os.getpid() and _pid_alive(pid):
            print(f"another backfill is running (pid {pid}); not starting a second one")
            return False
    except (OSError, ValueError):
        pass
    LOCK.write_text(str(os.getpid()))
    return True


def status() -> int:
    report = load_report()
    for sid, e in sorted(report.get("seasons", {}).items()):
        print(f"{e['label']}: {e['status']:<9} {e['games_included']} games, "
              f"{e['games_quarantined']} quarantined, +/- mismatches {len(e['pm_mismatches'])}, "
              f"{e['minutes']} min" + (f"  HELD: {'; '.join(e['blocking'])}" if e['blocking'] else ""))
    try:
        p = json.loads(PROGRESS.read_text(encoding="utf-8"))
        print(f"now: {p['season']} {p['done']}/{p['total']} games, "
              f"{p['quarantined']} quarantined, {p.get('requests', 0)} requests this run")
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    if LOG.exists():
        print("--- last log lines ---")
        print("\n".join(LOG.read_text(encoding="utf-8").splitlines()[-8:]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="first", default=FIRST_SEASON)
    parser.add_argument("--to", dest="last", default=None,
                        help="last season (default: the one before the current season)")
    parser.add_argument("--only", default=None, help="a single season id")
    parser.add_argument("--cache-root", default=str(ROOT / "cache" / "history"))
    parser.add_argument("--docs-dir", default=str(ROOT / config.DOCS_DIR))
    parser.add_argument("--push", action="store_true",
                        help="commit and push each season's processed data as it finishes")
    parser.add_argument("--redo", action="store_true",
                        help="recompute seasons that are already published")
    parser.add_argument("--status", action="store_true", help="print progress and exit")
    parser.add_argument("--publish-held", default=None, metavar="SEASON",
                        help="publish a held season's file as it is, after review")
    args = parser.parse_args(argv)
    if args.status:
        return status()
    if args.publish_held:
        return publish_held(args.publish_held, args)

    if not take_lock():
        return 1
    log = Log(LOG)
    meta = Fetcher(Path(args.cache_root), verbose=False)
    current = seasons.current_season(meta)
    last = args.last or max(s for s in seasons.all_seasons(meta) if s < current)
    todo = [args.only] if args.only else season_ids(args.first, last, meta)
    todo = [s for s in todo if s != current]     # the daily job owns the current season
    report = load_report()
    log(f"backfill start: {len(todo)} season(s) {todo[0]}..{todo[-1]}, push={args.push}")
    for sid in todo:
        if not args.redo and store.season_path(Path(args.docs_dir), sid).exists():
            log(f"== {store.season_label(sid)}: already published, skipping (--redo to recompute)")
            continue
        for attempt in range(1, 4):
            try:
                run_season(sid, args, log, report)
                break
            except KeyboardInterrupt:
                log("stopped by user; run the same command to resume")
                return 130
            except Exception as exc:   # network drop, API hiccup: retry, then move on
                log(f"  {sid} attempt {attempt} failed: {exc!r}")
                log("  " + traceback.format_exc().strip().replace("\n", "\n  ")[-1500:])
                time.sleep(60 * attempt)
        else:
            log(f"  {sid} gave up after 3 attempts; rerun to retry (cached work is kept)")
    log("backfill finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
