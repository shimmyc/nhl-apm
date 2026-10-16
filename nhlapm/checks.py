"""What "verified" means, defined once.

`verify.py` (the CLI) and `update.py` (the daily job) both call into here, so
there is a single definition of a good game and of a publishable season.

Per game -- a failure quarantines that one game:

* **Official TOI.** Each player's total on-ice time from the shift timeline
  must match the boxscore's published `toi` within TOLERANCE_SECONDS. The
  boxscore is a feed the pipeline does not otherwise use for time, so this is
  evidence from outside the fetch/parse/timeline path.
* **Official +/- rebuilt from first principles.** Play-by-play goals plus
  shift-derived on-ice sets must reproduce every skater's boxscore +/-. The
  one accepted exception is a goal scored on the exact second of a line
  change, where whole-second shift data cannot say which of the two skaters
  the NHL saw; it is accepted only if the mismatch is a pure swap between the
  skaters changing at that second, and it is logged as a diagnostic.
* **TOI identity, per team.** Five skaters per clean-5v5 second, so summed
  skater TOI must be exactly 5 x the game's clean seconds.
* **Zero-sum, per team.** Every counted goal puts exactly five skaters on the
  ice, so summed GD_on must be 5 x team GD and summed Adjusted must be zero.

Per season -- a failure blocks publishing:

* more than max(5 games, 2% of completed games) quarantined
  (config.quarantine_limit);
* any league-level or per-team identity broken in the *stored* data, i.e.
  after the season file has been written and read back;
* a team's included + quarantined games disagreeing with the standings
  feed's gamesPlayed (another feed the pipeline does not compute from).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config, parse, stats as stats_mod
from .parse import GameContext
from .records import GameRecord
from .timeline import GameTimeline

TOLERANCE_SECONDS = 2


@dataclass
class GameVerdict:
    failures: list[str] = field(default_factory=list)
    players_checked: int = 0
    worst_toi_delta: int = 0
    # +/- mismatches explained by a line change on the goal's exact second.
    boundary: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def boxscore_toi(boxscore: dict) -> dict[int, tuple[int, int]]:
    """playerId -> (toi seconds, shift count) as the NHL publishes them."""
    out: dict[int, tuple[int, int]] = {}
    for side in ("homeTeam", "awayTeam"):
        groups = boxscore.get("playerByGameStats", {}).get(side, {})
        for group in ("forwards", "defense", "goalies"):
            for entry in groups.get(group, []):
                toi = entry.get("toi")
                if toi:
                    out[entry["playerId"]] = (parse.clock_to_seconds(toi),
                                              entry.get("shifts", 0))
    return out


def rebuilt_plus_minus(context: GameContext, timeline: GameTimeline,
                       team_id: int) -> dict[int, int]:
    """Official +/- for one team's skaters, rebuilt from goals and shifts."""
    rebuilt, _ = _rebuild(context, timeline, team_id)
    return rebuilt


def _rebuild(context: GameContext, timeline: GameTimeline, team_id: int
             ) -> tuple[dict[int, int], dict[int, int]]:
    """(rebuilt +/-, boundary exposure).

    Boundary exposure counts, per skater, the eligible goals scored on the
    exact second one of this team's skaters left the ice and another came on.
    The shift feeds have whole-second resolution, so for those goals they
    cannot say which of the two the NHL saw on the ice.
    """
    skaters = {pid for pid, p in context.roster.items()
               if p.team_id == team_id and not p.is_goalie}
    rebuilt: dict[int, int] = {}
    exposure: dict[int, int] = {}
    for goal in context.goals:
        if goal.is_shootout:
            continue
        delta = 1 if goal.scoring_team_id == team_id else -1
        if stats_mod.is_penalty_shot(goal.situation_code):
            if delta == 1 and goal.scorer_id in skaters:
                rebuilt[goal.scorer_id] = rebuilt.get(goal.scorer_id, 0) + 1
            continue
        if not stats_mod.counts_for_plus_minus(
                goal.situation_code, goal.scoring_team_id == context.home_id):
            continue
        second = goal.absolute_second
        for pid in timeline.on_ice_at(second, skaters):
            rebuilt[pid] = rebuilt.get(pid, 0) + delta
        leaving = {p for p in timeline.on_ice_at(second, skaters)
                   if second + 1 not in timeline.occupancy.get(p, ())}
        arriving = {p for p in timeline.on_ice_at(second + 1, skaters)
                    if second not in timeline.occupancy.get(p, ())}
        if leaving and arriving:
            for pid in leaving | arriving:
                exposure[pid] = exposure.get(pid, 0) + 1
    return rebuilt, exposure


def verify_game(context: GameContext, timeline: GameTimeline, boxscore: dict,
                record: GameRecord) -> GameVerdict:
    verdict = GameVerdict()
    fail = verdict.failures

    # 1. per-player TOI against the boxscore
    official = boxscore_toi(boxscore)
    toi_bad = []
    for pid, seconds in timeline.occupancy.items():
        if pid not in official:
            continue
        verdict.players_checked += 1
        delta = len(seconds) - official[pid][0]
        verdict.worst_toi_delta = max(verdict.worst_toi_delta, abs(delta))
        if abs(delta) > TOLERANCE_SECONDS:
            player = context.roster.get(pid)
            toi_bad.append(f"{player.name if player else pid} {delta:+d}s")
    if toi_bad:
        fail.append(f"TOI vs boxscore off by >{TOLERANCE_SECONDS}s for "
                    f"{len(toi_bad)} player(s): {', '.join(toi_bad[:4])}")

    # 2. official +/- rebuilt from first principles. A mismatch is accepted
    #    only when it is explained by a line change on the goal's own second:
    #    every mismatched skater was one of the players changing then, by no
    #    more than the number of such goals, and the team's mismatches net to
    #    zero (a swap moves a point between two skaters; it creates none).
    pm_bad = []
    for team_id in (context.away_id, context.home_id):
        published = stats_mod.official_plus_minus(boxscore, context, team_id)
        rebuilt, exposure = _rebuild(context, timeline, team_id)
        diffs = {pid: published.get(pid, 0) - rebuilt.get(pid, 0)
                 for pid in set(published) | set(rebuilt)
                 if published.get(pid, 0) != rebuilt.get(pid, 0)}
        explained = bool(diffs) and sum(diffs.values()) == 0 and all(
            abs(d) <= exposure.get(pid, 0) for pid, d in diffs.items())
        for pid, d in diffs.items():
            player = context.roster.get(pid)
            text = (f"{player.name if player else pid} rebuilt "
                    f"{rebuilt.get(pid, 0):+d} vs {published.get(pid, 0):+d}")
            if explained:
                verdict.boundary.append(f"{context.ref.game_id} {text}")
            else:
                pm_bad.append(text)
    if pm_bad:
        fail.append(f"official +/- not reproduced for {len(pm_bad)} skater(s): "
                    f"{', '.join(pm_bad[:4])}")

    # 3 + 4. per-team identities on the rows that will be stored
    fail.extend(team_identity_failures(record))
    return verdict


def team_identity_failures(record: GameRecord) -> list[str]:
    out = []
    for team in (record.away, record.home):
        rows = [r for r in record.rows if r.team == team]
        if not rows:
            out.append(f"{team}: no skater rows")
            continue
        team_gd = rows[0].team_gd
        toi = sum(r.toi_5v5 for r in rows)
        if toi != 5 * record.clean_seconds:
            out.append(f"{team}: summed skater TOI {toi}s != 5 x {record.clean_seconds}s")
        gd_on = sum(r.gd_on for r in rows)
        if gd_on != 5 * team_gd:
            out.append(f"{team}: summed GD_on {gd_on:+d} != 5 x team GD {team_gd:+d}")
        adjusted = sum(r.gd_on - r.expected_game for r in rows)
        if abs(adjusted) > config.ZERO_SUM_TOLERANCE:
            out.append(f"{team}: summed Adjusted {adjusted:+.6f}, not zero")
    return out


# -- season level ----------------------------------------------------------

@dataclass
class SeasonVerdict:
    blocking: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)   # what was checked, for the log

    @property
    def ok(self) -> bool:
        return not self.blocking


def _concentration(season) -> list[str]:
    """Where a past season's quarantined games cluster: one club or one week."""
    import datetime
    club_total: dict[str, int] = {}
    club_bad: dict[str, int] = {}
    week_total: dict[tuple, int] = {}
    week_bad: dict[tuple, int] = {}
    def week(date):
        return datetime.date.fromisoformat(date).isocalendar()[:2]
    for g in season.games.values():
        for t in (g.away, g.home):
            club_total[t] = club_total.get(t, 0) + 1
        week_total[week(g.date)] = week_total.get(week(g.date), 0) + 1
    for e in season.excluded:
        for t in (e.away, e.home):
            club_total[t] = club_total.get(t, 0) + 1
            club_bad[t] = club_bad.get(t, 0) + 1
        w = week(e.date)
        week_total[w] = week_total.get(w, 0) + 1
        week_bad[w] = week_bad.get(w, 0) + 1
    out = []
    for t, n in club_bad.items():
        if n > config.HISTORICAL_CLUB_SHARE * club_total[t]:
            out.append(f"{t}: {n} of {club_total[t]} games quarantined")
    for w, n in week_bad.items():
        if n > config.HISTORICAL_WEEK_SHARE * week_total[w]:
            out.append(f"week {w[0]}-W{w[1]:02d}: {n} of {week_total[w]} games quarantined")
    return out


def verify_season(season, standings_games: dict[str, int] | None,
                  historical: bool = False) -> SeasonVerdict:
    """League-level checks over a `store.Season`, normally one just read back.

    `historical` swaps the current season's tight gate (max(5 games, 2%)) for
    the past-season one: block only when failures are 20%+ of the season or
    cluster on one club or week."""
    verdict = SeasonVerdict()
    games = season.games
    rows = season.rows
    excluded = season.excluded
    completed = len(games) + len(excluded)

    share = len(excluded) / completed if completed else 0.0
    if historical:
        verdict.lines.append(
            f"games included {len(games)}, quarantined {len(excluded)} "
            f"({share:.1%} of {completed}; past-season limit {config.HISTORICAL_QUARANTINE_SHARE:.0%} "
            f"and no club/week cluster)")
        if share >= config.HISTORICAL_QUARANTINE_SHARE:
            verdict.blocking.append(
                f"{len(excluded)} of {completed} games quarantined ({share:.1%}) -- "
                f"systemic: at or above {config.HISTORICAL_QUARANTINE_SHARE:.0%}")
        for line in _concentration(season):
            verdict.blocking.append(f"quarantined games cluster -- {line}")
    else:
        limit = config.quarantine_limit(completed)
        verdict.lines.append(
            f"games included {len(games)}, quarantined {len(excluded)} "
            f"({share:.1%} of {completed}; limit {limit} games)")
        if len(excluded) > limit:
            verdict.blocking.append(
                f"{len(excluded)} of {completed} games quarantined ({share:.1%}) -- "
                f"above the systemic limit of {limit}")
    if not games:
        verdict.blocking.append("no games included")
        return verdict

    # League-wide identities.
    total_toi = sum(r.toi_5v5 for r in rows)
    total_clean = sum(g.clean_seconds for g in games.values())
    total_gd_on = sum(r.gd_on for r in rows)
    total_adj = sum(r.gd_on - r.expected_game for r in rows)
    total_gf = sum(r.gf_on for r in rows)
    total_ga = sum(r.ga_on for r in rows)
    goals = sum(g.home_gf + g.away_gf for g in games.values())
    verdict.lines.append(
        f"league: skater TOI {total_toi}s vs 10 x clean {10 * total_clean}s; "
        f"sum GD_on {total_gd_on:+d}; sum Adjusted {total_adj:+.2e}; "
        f"on-ice GF {total_gf} / GA {total_ga} vs 5 x {goals} goals")
    if total_toi != 10 * total_clean:
        verdict.blocking.append("league skater TOI is not 10 x clean 5v5 time")
    if total_gd_on != 0:
        verdict.blocking.append(f"league sum of GD_on is {total_gd_on:+d}, not 0")
    if abs(total_adj) > config.ZERO_SUM_TOLERANCE * max(len(rows), 1):
        verdict.blocking.append(f"league sum of Adjusted is {total_adj:+.6f}, not 0")
    if not total_gf == total_ga == 5 * goals:
        verdict.blocking.append("on-ice GF/GA do not equal 5 x counted goals")

    # Per game per team, re-run on what was actually written.
    by_game: dict[int, list] = {}
    for r in rows:
        by_game.setdefault(r.game_id, []).append(r)
    bad_games = []
    for gid, game in games.items():
        failures = team_identity_failures(_as_record(game, by_game.get(gid, [])))
        if failures:
            bad_games.append(f"{gid}: {failures[0]}")
    orphans = set(by_game) - set(games)
    if orphans:
        verdict.blocking.append(f"{len(orphans)} game id(s) have rows but no game entry")
    if bad_games:
        verdict.blocking.append(f"{len(bad_games)} stored game(s) break a per-game "
                                f"identity: {'; '.join(bad_games[:3])}")

    by_team: dict[str, list] = {}
    for r in rows:
        by_team.setdefault(r.team, []).append(r)
    bad_teams = []
    for team, team_rows in sorted(by_team.items()):
        adj = sum(r.gd_on - r.expected_game for r in team_rows)
        gd_on = sum(r.gd_on for r in team_rows)
        team_gd = sum(g.home_gf - g.away_gf if g.home == team else g.away_gf - g.home_gf
                      for g in games.values() if team in (g.home, g.away))
        if abs(adj) > config.ZERO_SUM_TOLERANCE * len(team_rows) or gd_on != 5 * team_gd:
            bad_teams.append(f"{team} sum Adjusted {adj:+.6f}, GD_on {gd_on:+d} "
                             f"vs 5 x {team_gd:+d}")
    verdict.lines.append(f"per-team zero-sum: {len(by_team) - len(bad_teams)}/{len(by_team)} OK; "
                         f"per-game identities: {len(games) - len(bad_games)}/{len(games)} OK")
    if bad_teams:
        verdict.blocking.append("per-team zero-sum broken: " + "; ".join(bad_teams[:4]))

    # Games per team against the standings feed.
    if standings_games is not None:
        ours: dict[str, int] = {}
        for g in games.values():
            ours[g.home] = ours.get(g.home, 0) + 1
            ours[g.away] = ours.get(g.away, 0) + 1
        for item in excluded:
            ours[item.home] = ours.get(item.home, 0) + 1
            ours[item.away] = ours.get(item.away, 0) + 1
        mismatched = [f"{t} ours {ours.get(t, 0)} vs standings {n}"
                      for t, n in sorted(standings_games.items()) if ours.get(t, 0) != n]
        verdict.lines.append(
            f"games per team vs standings gamesPlayed: "
            f"{len(standings_games) - len(mismatched)}/{len(standings_games)} agree")
        if mismatched:
            verdict.blocking.append("games per team disagree with standings: "
                                    + "; ".join(mismatched[:4]))
    return verdict


def _as_record(game, rows) -> GameRecord:
    record = GameRecord(game_id=game.game_id, date=game.date, home=game.home,
                        away=game.away, clean_seconds=game.clean_seconds,
                        home_gf=game.home_gf, away_gf=game.away_gf, source=game.source)
    record.rows = list(rows)
    return record


# -- season +/- against the NHL's season report ------------------------------

@dataclass
class SeasonPmCheck:
    players_checked: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def share(self) -> float:
        return len(self.mismatches) / self.players_checked if self.players_checked else 0.0


def season_plus_minus(season, summary: dict, quarantined_pm: dict[int, int],
                      names: dict[int, str] | None = None) -> SeasonPmCheck:
    """Each skater's summed per-game Official_PM vs the NHL's season +/-.

    `summary` is the stats API skater/summary report for the season: one row
    per player with every club combined, a feed this pipeline never computes
    from. Our side is the per-game boxscore +/- over included games plus, for
    quarantined games, the same boxscore +/- (`quarantined_pm`) -- a game
    excluded from the 5v5 numbers still happened, and the NHL counted it.
    """
    names = names or {}
    ours: dict[int, int] = dict(quarantined_pm)
    for r in season.rows:
        ours[r.player_id] = ours.get(r.player_id, 0) + r.official_pm
    theirs = {row["playerId"]: (row.get("plusMinus") or 0, row.get("skaterFullName", ""))
              for row in summary.get("data", [])}
    result = SeasonPmCheck()
    for pid in sorted(set(ours) | set(theirs)):
        mine = ours.get(pid, 0)
        nhl, name = theirs.get(pid, (0, names.get(pid, str(pid))))
        if pid not in ours and nhl == 0:
            continue    # listed by the NHL, never on the ice at 5v5 or in a box: nothing to compare
        result.players_checked += 1
        if mine != nhl:
            where = "" if pid in theirs else " (not in the NHL report)"
            result.mismatches.append(f"{name or pid} ours {mine:+d} vs NHL {nhl:+d}{where}")
    return result
