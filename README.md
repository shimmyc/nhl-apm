# Adjusted plus-minus, 5v5 with both goalies in net

Adjusted plus-minus for every NHL skater, from NHL public data (no key
required). Current season `20262027`, rebuilt daily and published as a static
page that filters and aggregates in the browser.

```
python update.py                     # the daily job: new games -> verify each -> season checks -> publish
python update.py --dry-run           # compute and verify, write nothing
python update.py --rebuild           # recompute every game from the raw cache (refetching what is missing)
python main.py --league --min-toi 50 # league top/bottom 10 from the published data
python main.py --team BOS            # one club, computed from the feeds, with a CSV
python verify.py                     # re-verify every completed game league-wide
python verify.py --team CHI --goals  # one club, with the on-ice skaters for every 5v5 goal
python verify.py --stored            # season-level checks on the published file only
```

Stdlib only. Python 3.9+ (uses `X | None` annotations under
`from __future__ import annotations`).

## Layout

| Module | Responsibility |
| --- | --- |
| `nhlapm/config.py` | Season, period grid, the verified 5v5 situation code, gate thresholds |
| `nhlapm/fetch.py` | HTTP + verbatim disk cache under `cache/`, ~1 request/sec, retry |
| `nhlapm/parse.py` | Payloads -> typed records; all times to absolute game seconds |
| `nhlapm/html_toi.py` | Second shift source: the HTML Time On Ice reports |
| `nhlapm/loader.py` | Picks the shift source for a game; the one place that decides |
| `nhlapm/timeline.py` | Second-by-second on-ice state; clean-5v5 mask |
| `nhlapm/records.py` | **The stored unit: one row per skater per game** |
| `nhlapm/checks.py` | What "verified" means, per game and per season -- defined once |
| `nhlapm/pipeline.py` | League discovery, per-game compute + verify + quarantine, metadata |
| `nhlapm/store.py` | Season file / players.json / manifest, compact on disk |
| `nhlapm/aggregate.py` | Folds player-game rows into one line per skater (the page's twin) |
| `nhlapm/site.py` | Renders `docs/index.html`, the client-side page |
| `nhlapm/stats.py`, `output.py` | Goal-eligibility rules; CSV and console table |
| `update.py` / `main.py` / `verify.py` | Daily job / terminal reports / independent checks |

## Data model: one row per skater per game

Every published number is a sum over player-game rows, so any filter -- a team,
a date range, home games, a skater's last ten -- is a selection followed by a
sum. Each row holds the game, the skater's team and opponent, home/away,
`TOI_5v5`, `GF_on`, `GA_on`, the NHL's `Official_PM` for that game, and the
game's team-level 5v5 time and goals. From those:

```
Expected_game = team 5v5 GD in that game x (player 5v5 TOI / team 5v5 TOI in that game)
Expected      = sum of Expected_game over the selected games
Adjusted      = GD_on - Expected
Rel           = GD_on - GD_off x (TOI_on / TOI_off)   off-ice summed over the same games
```

**What changed from the season-ratio method.** Expected used to be
`Team_GD_season x TOI_season / Team_TOI_season`, with team totals over every
team game -- including games the skater missed -- so a skater was charged for
results they were not dressed for, and the number could not be split by date or
venue. Built per game, a skater is only measured against the games they played.
Over the same seven Blackhawks games (inputs identical to the old run), the
mean change in Adjusted was 0.18 goals and the largest 0.50 (Bowen Byram,
-0.92 to -0.42: the one game Byram missed was a 0-GD game, so the old method
spread the team's -9 over minutes that included it). One-game skaters move
most: Connor Bedard's single game was a 0-GD game for the team, so the
Expected is 0.00 rather than -0.45. Rel changes the same way, for the same
reason.

**The zero-sum identities hold at every grain.** Per team per game, five
skaters are on the ice for every clean-5v5 second and every counted goal, so

```
sum(skater TOI)    = 5 x the game's clean-5v5 seconds
sum(GD_on)         = 5 x team 5v5 GD
sum(Adjusted_game) = 0
```

and because they hold per game they hold for every union of games: per team,
per season, per date range. The season-ratio method only balanced over a
whole team-season.

Expected is never stored: it is a pure function of the stored integers, so
the file cannot disagree with the definition.

**Metadata, verified field names (2026-10-15):**

- Teams: `GET api-web.nhle.com/v1/standings/now` -> `teamAbbrev.default`,
  `teamName.default`, `conferenceAbbrev`/`conferenceName`,
  `divisionAbbrev`/`divisionName`, `gamesPlayed`. Stored per season, since
  alignment changes between seasons.
- Players: `GET api.nhle.com/stats/rest/en/skater/bios?limit=-1&cayenneExp=seasonId=...`
  -> `skaterFullName`, `positionCode` (C/L/R/D), `shootsCatches`, `birthDate`.
  One request for every skater who has played. The api-web roster endpoint
  (`/v1/roster/{team}/{season}`) has the same fields (`positionCode`,
  `shootsCatches`, `birthDate`) but only lists current rosters, so traded and
  waived players go missing; it is not used.
- Rookies: `skater/summary` with `isRookie="1"` in the `cayenneExp`. It is the
  NHL's own definition, not an age rule: Connor Bedard is in the 2023-24 rookie
  list and not in 2026-27's; players who debuted with a few games in earlier
  seasons still are. Stored per season.

## Storage

The raw API cache is **not committed** (it runs ~0.5 MB a game, ~650 MB a
season). It lives in the GitHub Actions cache; `cache/` is gitignored.

Committed, under `docs/data/`:

| File | Contents |
| --- | --- |
| `index.json` | Manifest: seasons available, default season, players file |
| `20262027.json` | One season: teams, games, player-game rows, rookies, exclusions |
| `players.json` | Player id -> name, position, shoots, birthdate (season-independent) |

Season files are arrays, not objects, with each table's column order declared
once in a `cols` list beside it -- the file documents itself without repeating
keys 50,000 times. At 106 games the season file is 104 KB (28 KB gzipped); a
full 1,312-game season projects to about 1.3 MB (~350 KB gzipped), well under
the 3 MB budget.

**Growing it later.** A past season is one more `{season}.json` plus a manifest
entry; the page's season picker appears as soon as there are two. A custom date
range is a filter over rows that already carry dates. A player career page is a
fold over the season files keyed by player id, which is why identity lives in
`players.json` and season facts (team, rookie status) live in the season file.

## The page

`docs/index.html` is a static shell. It loads the manifest, then the season
file and `players.json`, and folds the rows the filters select in the browser
-- the same sums as `aggregate.py`. A browser test compared the page's output
against the Python aggregation across four views (league at 50 min, CHI, CHI
home, Central last 5): every skater matched.

Filters: scope (league / conference / division / team), position (all,
forwards, defense, C, LW, RW), games (season to date, each skater's last 5 /
10 / 20 within the other filters, or custom dates), minimum 5v5 TOI (default
50), home / away / both, rookies only, name search (accent-insensitive), and
totals versus per-60. Per 60 turns GD_on, Expected, Adjusted and Rel into rates
of 5v5 time; Official_PM and NonClean stay totals because they are not 5v5
quantities.

A skater traded mid-season is one combined row league-wide, labelled in the
order they played for each club (`PIT/EDM`), and a separate row per club when the
scope is that club.

**Shareable views.** Every filter and the sort live in the query string, e.g.
`?team=CHI&pos=D&venue=away&per60=1&min=20`. Opening the link restores the
view.

**Speed on a phone.** Measured at 390 px with 6x CPU throttling: the first
render with data took about 0.4 s. Dropping the minimum TOI to 0 re-folded
all 678 skaters and re-rendered in about 0.1 s. Rows render as one string into
`innerHTML`, capped at 100 until "Show all", and the name search is debounced.

`Adjusted` is a polarity encoding: a signed numeral in primary ink, a wash,
and a bar that grows right for positive and left for negative from a centre
tick. On a phone the Player column is pinned left and Adjusted right.

Green/red is the hockey convention and is what ships, but measured with the
palette validator green `#0ca30c` against red `#d03b3b` separates by **deutan
Delta E 4.1**, below the usable floor. The column stays readable for a
colourblind reader because hue is never the only cue -- the sign and the bar's
direction carry polarity on their own. To make it safe on hue alone, set
`ACCENT_POSITIVE` in `nhlapm/config.py` to `#2a78d6` (deutan Delta E 23.8).
Dark mode is its own set of steps, not an inversion, and the washes are opaque
because a sticky cell must not be see-through.

## Verification at league scale

Definitions live in `nhlapm/checks.py`; `verify.py` and `update.py` both call
them.

**Per game** -- a failing game is **quarantined**: excluded from every total
and listed on the page under "games excluded", never fatal on its own.

- Each player's on-ice time from the shift timeline must match the boxscore's
  published `toi` within 2 s. The boxscore is a feed the pipeline does not
  otherwise use for time.
- Each skater's official +/- must be rebuilt exactly from play-by-play goals
  plus shift-derived on-ice sets.
- Per team: summed skater TOI = 5 x clean seconds; summed GD_on = 5 x team GD;
  summed Adjusted = 0.

**Per season** -- these block publishing:

- more than 2% of completed games quarantined;
- any league-level identity broken *in the file as written and read back*:
  league TOI = 10 x clean time, league GD_on = 0, league Adjusted = 0,
  on-ice GF = GA = 5 x goals, every stored game's per-team identities, every
  team's zero-sum;
- any team's included + quarantined games disagreeing with the standings
  feed's `gamesPlayed`.

Tested on a scratch copy: corrupting one cached boxscore quarantined that game
and published with it listed (2 of 107, 1.9%); corrupting two blocked publish
(3 of 107, 2.8%) with every published file byte-identical; adding one phantom
goal to one stored row tripped five separate season identities.

Early in a season 2% is a small number of games: at 107 games, three failures
block. That is the rule as specified.

### What league scale found

The first full backfill (107 games) failed the gate: 8 games, 7.5%. Each was
traced to a cause before anything changed.

- **Phantom shifts in `shiftcharts` (5 games).** The JSON feed carries extra
  shift rows the boxscore does not count -- e.g. game `2026020077`, where ten
  players are 4-27 s over and show 3-4 more shifts than the boxscore. The HTML
  report for the same games matched the boxscore exactly, seconds and shift
  counts. So the shift source is now tried in order -- `shiftcharts`, then the
  HTML reports -- and the first that passes **every** per-game check is used.
  Both face the same checks, so this is a second source, not a lower bar.
- **Penalty shots (1 game).** A penalty-shot goal is coded with a lone shooter
  against a lone goalie (`1010`). In `2026020065` Jack Eichel's penalty-shot
  goal moved Eichel's published +/- by +1 and nobody else's, though nine other
  skaters were on the ice per the shifts. The rebuild now credits the shooter
  alone. This affects only the check; `Official_PM` is always the boxscore's.
- **A line change on the goal's own second (1 game).** In `2026020015` a
  shorthanded goal came on the exact second Connor Murphy's shift ended and
  Ryan Shea's began. Whole-second shift data cannot say which of them the NHL
  saw; it credited Shea, our convention credits Murphy. Such a mismatch is
  accepted only when every mismatched skater was one of those changing at a
  goal's second, by no more than the number of such goals, netting to zero
  for the team -- a pure swap. It is logged as a non-blocking diagnostic
  (`pm_line_change_on_goal_second`). TOI and the zero-sum identities still
  apply in full.
- **A real hole (1 game, quarantined).** `2026020023` OTT@TOR: shiftcharts is
  empty and the HTML report is missing a Toronto skater for a 5v5 goal (the
  shifts show four). It stays out, and is refetched every run in case the NHL
  fixes it.

After those: 106 included, 1 quarantined (0.9%), every season check passing,
and games-per-team agreeing with the standings for all 32 clubs. 52 of the 106
included games come from the HTML reports, because `shiftcharts` is empty for
them.

## Method

### The `situationCode` digit order, verified

The order is **`[away goalies][away skaters][home skaters][home goalies]`**.
Confirmed against game `2026020098` (PIT @ CHI, 2026-10-13), where CHI is home,
by two independent events:

1. **A known power-play goal.** Event 376, `situationCode` `1451`, scored by CHI
   (`eventOwnerTeamId` 16 = home). The NHL's own highlight slug for it reads
   `chi-kantserov-scores-ppg-...`. Read as away-first, `1451` = PIT 4 skaters,
   CHI 5 -- CHI on the power play, which agrees with "ppg". Read home-first it
   would mean CHI shorthanded, which contradicts it.
2. **A known empty-net goal.** Event 1017, `situationCode` `0651`, CHI goal at
   19:50 of the 3rd with PIT trailing. The leading digit is `0` and the event
   has **no `goalieInNetId` field at all**. The absent goalie is Pittsburgh's,
   and Pittsburgh is the away team, so the leading digit tracks the away
   goalie. The shift feed agrees independently: PIT's goalie logged 57:57.

Therefore `1551` = five skaters a side, both goalies in net. That is the only
code counted toward `GD_on`.

### Time axis and on-ice rule

Every period is a 20-minute block, so absolute second =
`(period - 1) * 1200 + elapsed`. Regular-season OT is 3v3 and never qualifies;
the shootout is excluded by `periodType == "SO"`.

Second *t* denotes the interval `(t-1, t]`. A shift `(start, end]` occupies
seconds `start+1 .. end`, the same rule as for goals --
`shift_start < goal_time <= shift_end` -- so the set occupying second *G* **is**
the set on ice for a goal at *G*.

The shiftcharts feed mixes goal rows into the shift array (`typeCode` 505, an
`eventDescription`, `duration: null`); only `typeCode` 517 rows are shifts.
Each player's shifts are collapsed into a set of seconds first, so duplicated
or overlapping rows cannot inflate a team's skater count. A second is clean 5v5
only when both teams show exactly 5 skaters + 1 goalie.

### Shift sources

`loader.load_game_context` prefers the JSON `shiftcharts` feed and parses the
HTML Time On Ice reports (`TV`/`TH` + the last six digits of the game id) when
it returns zero rows; the pipeline also turns to the HTML reports when
shiftcharts fails verification. Both produce the same `Shift` records; the
source is recorded per game and shown on the page.

The reports carry no player ids, so players are joined to the play-by-play
roster on **(team, sweater number)**, with the surname as a cross-check. Two
traps in that format, both of which produced wrong numbers before being fixed:

- **Per-period summary rows look like shifts.** A shift row is identified by
  requiring the full `elapsed / remaining` pair, slash included, which no
  summary row has; accepting a bare `MM:SS` adds a phantom shift per period.
- **Surnames contain spaces.** `Player.last_name` is kept as its own field so
  `DEL MASTRO, ETHAN` compares exactly.

### The plus-minus eligibility rule

`stats.counts_for_plus_minus` compares **total players per side (skaters +
goalie)**, not skater counts. A team that has pulled its goalie ices six
skaters without holding a man advantage, so a goal it scores does earn plus.

### Columns

| Column | Meaning |
| --- | --- |
| `TOI_5v5` | Minutes on ice at clean 5v5 |
| `GD_on` | Clean-5v5 goals for minus against while on ice |
| `Official_PM` | The NHL's own +/-, all situations, over the same games |
| `NonClean` | `Official_PM - GD_on`: empty nets, 4v4, shorthanded goals... |
| `Expected` | Sum over games of team 5v5 GD x share of that game's 5v5 time |
| `Adjusted` | `GD_on - Expected` -- the default sort |
| `Rel` | `GD_on - GD_off x (TOI_on / TOI_off)` over the games played; blank with no off-ice time |
| `GD_on/60` | Rate form of GD_on |

`GP` counts games in the selection in which the skater took at least one shift.

## Automation

`.github/workflows/update.yml` runs daily at 11:00 UTC -- 06:00 Central on CDT,
05:00 once CST starts -- and from the **Run workflow** button, which also
offers a `rebuild` switch.

1. Restore the raw cache from `actions/cache` (newest key by prefix).
2. `update.py`: walk the weekly schedule for completed games; reuse every game
   already in the season file; compute and verify only new games, plus
   quarantined games, whose payloads are refetched fresh so an NHL correction
   is picked up; run the season checks; publish or block.
3. Save the raw cache under the run's own key -- `if: always()`, so a backfill
   that times out resumes where it stopped -- skipped when no game payload was
   downloaded.
4. Commit `docs/` and the Blackhawks CSV.

A no-op day costs about ten requests (schedule, standings, bios, rookies, and
any quarantined game). Requests are throttled to about one a second, so a
cold full-season backfill is roughly an hour of a 240-minute job budget.
