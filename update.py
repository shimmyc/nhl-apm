"""Daily update: fetch new games league-wide, verify, then publish.

    python update.py              # the job CI runs
    python update.py --dry-run    # compute and verify, write nothing
    python update.py --rebuild    # recompute every game from the raw cache

Order matters:

1. every completed game is verified on its own as it is computed; a game that
   fails is quarantined (excluded and listed on the page), not fatal;
2. the season file is serialised and read back, and the league-level checks
   run on what was read back -- so they test the file that will ship;
3. only if those pass is anything written. A systemic failure (too many games
   quarantined, an identity broken) leaves the published site untouched and
   exits non-zero for CI to report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from nhlapm import aggregate, careers, checks, config, output, pipeline, seasons, site, store
from nhlapm.fetch import Fetcher

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", default=None,
                        help="season id, e.g. 20262027 (default: the current season, "
                             "asked of the NHL)")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache"))
    parser.add_argument("--docs-dir", default=str(ROOT / config.DOCS_DIR))
    parser.add_argument("--dry-run", action="store_true",
                        help="run everything but write no files")
    parser.add_argument("--rebuild", action="store_true",
                        help="recompute every game instead of reusing the season file")
    parser.add_argument("--no-html-fallback", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="one line per run, not per game")
    return parser.parse_args(argv)


def _github_output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def carried_diagnostics(previous: store.Season | None, reused_ids: set[int],
                        fresh: dict[str, list[str]]) -> dict[str, list[str]]:
    """Feed-disagreement notes for every included game: fresh plus carried over."""
    merged: dict[str, list[str]] = {k: list(v) for k, v in fresh.items()}
    old = (previous.checks.get("diagnostics", {}) if previous else {}) or {}
    for key, entries in old.items():
        for entry in entries:
            try:
                gid = int(entry.split()[0])
            except (ValueError, IndexError):
                continue
            if gid in reused_ids:
                merged.setdefault(key, []).append(entry)
    return {k: sorted(set(v)) for k, v in merged.items()}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    docs_dir = Path(args.docs_dir)
    started = datetime.now(timezone.utc)
    fetcher = Fetcher(Path(args.cache_dir), verbose=not args.quiet)
    if args.season is None:
        args.season = seasons.current_season(fetcher, started.date())
    print(f"nhl-apm update | season {args.season} | league-wide")
    previous = None if args.rebuild else store.load_season(docs_dir, args.season)
    if previous is None and not args.rebuild:
        print("  no season file yet: this run is a backfill")

    # -- 1. per-game: compute + verify, quarantining failures --------------
    log = (lambda _m: None) if args.quiet else print
    run = pipeline.run_league(fetcher, args.season, previous, rebuild=args.rebuild,
                              html_fallback=not args.no_html_fallback, log=log)
    season = run.season
    season.updated = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not season.games and not season.excluded and previous is None:
        # A season that has started on paper but has no final game yet (the
        # morning of opening night). Nothing to verify or publish.
        print(f"  no completed games yet in {args.season}; nothing to publish")
        _github_output(published="false", fetched=fetcher.game_calls)
        return 0

    players, missing = pipeline.collect_player_metadata(
        fetcher, args.season, season, store.load_players(docs_dir))
    if missing:
        pipeline.fill_from_rosters(fetcher, season, players, missing)
        print(f"  {len(missing)} skater(s) not in the bios report yet; "
              f"named from the play-by-play roster")

    reused_ids = {gid for gid in season.games if gid not in set(run.computed)}
    diagnostics = carried_diagnostics(previous, reused_ids, run.diagnostics)
    season.checks = {
        "games_included": len(season.games),
        "games_quarantined": len(season.excluded),
        "diagnostics": diagnostics,
    }

    print(f"  games: {len(season.games)} included, {len(season.excluded)} quarantined, "
          f"{run.reused} reused from the season file, {len(run.computed)} computed, "
          f"{run.pending_games} not yet played")
    print(f"  network: {fetcher.api_calls} request(s) ({fetcher.game_calls} for game "
          f"payloads), {fetcher.cache_hits} cache hit(s)")
    for item in season.excluded:
        print(f"    quarantined {item.game_id} {item.date} {item.away}@{item.home}: "
              f"{item.reason}")
    for line in run.fallbacks:
        print(f"    HTML fallback after failed verification -- {line}")
    if run.recovered:
        print(f"  recovered from quarantine: {', '.join(map(str, run.recovered))}")
    for key, entries in sorted(diagnostics.items()):
        print(f"  diagnostic (non-blocking) {key}: {len(entries)}")

    # -- 2. serialise, read back, verify what will ship --------------------
    season_text = store.dumps_compact(store.season_to_json(season))
    shipped = store.season_from_json(json.loads(season_text))
    verdict = checks.verify_season(shipped, run.standings_games)
    print("\nSeason checks (on the serialised file, read back):")
    for line in verdict.lines:
        print(f"  {line}")
    if not verdict.ok:
        for line in verdict.blocking:
            print(f"  BLOCKING: {line}", file=sys.stderr)
        print("\nPUBLISH BLOCKED. The published site is unchanged.", file=sys.stderr)
        _github_output(published="false", fetched=fetcher.game_calls)
        return 1
    print("  all season checks passed")

    players_text = store.dumps_compact(store.players_to_json(
        {pid: players[pid] for pid in players}))
    page = site.render_page(site.SiteData(season=shipped, generated_at_utc=started))

    if args.dry_run:
        print(f"\nDRY RUN: season file {len(season_text):,} bytes, players "
              f"{len(players_text):,} bytes, page {len(page):,} bytes; nothing written.")
        return 0

    # -- 3. publish -------------------------------------------------------
    data_dir = store.data_dir(docs_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store.season_path(docs_dir, args.season).write_text(season_text, encoding="utf-8")
    (data_dir / "players.json").write_text(players_text, encoding="utf-8")
    (data_dir / careers.FILE_NAME).write_text(
        store.dumps_compact(careers.update(docs_dir, shipped)), encoding="utf-8")
    manifest = store.manifest_to_json(docs_dir, shipped)
    (data_dir / "index.json").write_text(store.dumps_compact(manifest), encoding="utf-8")
    (docs_dir / "index.html").write_text(page, encoding="utf-8")
    (docs_dir / "player.html").write_text(site.render_player_page(), encoding="utf-8")
    (docs_dir / ".nojekyll").write_text("", encoding="utf-8")

    # The Blackhawks CSV keeps its old address, now computed the new way.
    hawks = aggregate.aggregate([r for r in shipped.rows if r.team == config.DEFAULT_PAGE_TEAM],
                                players)
    csv_path = output.write_csv(hawks, ROOT / config.PRIMARY_CSV_NAME)
    shutil.copyfile(csv_path, docs_dir / config.PRIMARY_CSV_NAME)

    print(f"\nPublished {store.season_path(docs_dir, args.season)} "
          f"({len(season_text):,} bytes), players.json ({len(players_text):,} bytes), "
          f"index.html ({len(page):,} bytes)")
    _github_output(published="true", fetched=fetcher.game_calls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
