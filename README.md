# Adjusted plus-minus, 5v5 with both goalies in net

Computes adjusted plus-minus for a club's skaters from NHL public data (no key
required). Default target: Chicago Blackhawks, season `20262027`.

```
python update.py                     # the daily job: fetch -> verify -> publish docs/index.html
python update.py --dry-run           # compute and verify, write nothing
python main.py                       # Blackhawks, 2026-27
python main.py --team BOS            # any club
python main.py --no-html-fallback    # JSON shiftcharts only
python main.py --retry-empty-shifts  # re-ask the API for games cached with zero shift rows
python main.py --min-toi 20          # hide low-TOI skaters from the console table only
python verify.py                     # validate TOI and official +/- against other feeds
python verify.py --goals             # also list the on-ice five for every qualifying goal
```

Stdlib only. Python 3.9+ (uses `X | None` annotations under
`from __future__ import annotations`).

## Layout

| Module | Responsibility |
| --- | --- |
| `nhlapm/config.py` | Season, team, period grid, the verified 5v5 situation code |
| `nhlapm/fetch.py` | HTTP + verbatim disk cache under `cache/`, retry/throttle |
| `nhlapm/parse.py` | Payloads -> typed records; all times to absolute game seconds |
| `nhlapm/html_toi.py` | Fallback shift source: the HTML Time On Ice reports |
| `nhlapm/loader.py` | Picks the shift source for a game; the one place that decides |
| `nhlapm/timeline.py` | Second-by-second on-ice state; clean-5v5 mask |
| `nhlapm/stats.py` | Season aggregation, derived metrics, feed cross-checks |
| `nhlapm/output.py` | CSV, console table, validation summary |
| `nhlapm/pipeline.py` | Runs one club end to end; shared by the CLI and the site |
| `nhlapm/site.py` | Renders `docs/index.html` |
| `main.py` | CLI wiring |
| `update.py` | Daily job: pipeline -> verify gate -> publish |
| `verify.py` | Independent checks against the boxscore feed |

Nothing in `fetch`/`parse`/`timeline` is Blackhawks-specific; only `stats`
filters to one club. Adding clubs is one line -- `config.TEAMS` -- which the
pipeline, the verify gate and the page all iterate. Verified end to end with
`--teams CHI,STL`: both clubs computed and verified independently, and games
the two share were served from cache rather than refetched. A RAPM model would
consume `timeline.occupancy` plus `clean_5v5` directly as its design matrix —
that is why occupancy is kept as per-player second sets rather than collapsed
into totals.

## The `situationCode` digit order, verified

The order is **`[away goalies][away skaters][home skaters][home goalies]`**.
Confirmed against game `2026020098` (PIT @ CHI, 2026-10-13), where CHI is home,
by two independent events:

1. **A known power-play goal.** Event 376, `situationCode` `1451`, scored by CHI
   (`eventOwnerTeamId` 16 = home). The NHL's own highlight slug for it reads
   `chi-kantserov-scores-ppg-...`. Read as away-first, `1451` = PIT 4 skaters,
   CHI 5 — CHI on the power play, which agrees with "ppg". Read home-first it
   would mean CHI shorthanded, which contradicts it.
2. **A known empty-net goal.** Event 1017, `situationCode` `0651`, CHI goal at
   19:50 of the 3rd with PIT trailing. The leading digit is `0` and the event
   has **no `goalieInNetId` field at all** — the goalie the puck beat was not
   there. The absent goalie is Pittsburgh's, and Pittsburgh is the away team, so
   the leading digit tracks the away goalie. The shift feed agrees
   independently: PIT's goalie logged 57:57, i.e. pulled at 17:57 of the 3rd.

Therefore `1551` = five skaters a side, both goalies in net. That is the only
code counted toward `GD_on`.

## Method

**Time axis.** Every period is a 20-minute block, so absolute second =
`(period - 1) * 1200 + elapsed`. Shift rows and plays both carry
`period` + `MM:SS`, so one axis serves both. Regular-season OT is shorter than
its block but is 3v3 and never qualifies; the shootout is excluded by
`periodType == "SO"`.

**Half-open seconds, one convention for both uses.** Second *t* denotes the
interval `(t-1, t]`. A shift with bounds `(start, end]` occupies seconds
`start+1 .. end`, which is the same rule the spec gives for goals —
`shift_start < goal_time <= shift_end`. So the set of players occupying second
*G* **is** the set on ice for a goal at *G*; the timeline and the on-ice rule
cannot drift apart.

**Shift rows only.** The shiftcharts feed mixes goal rows into the same array.
Real shifts carry `typeCode` 517; goal rows carry 505, an `eventDescription`
like `EVG`/`PPG`/`EN`, `duration: null`, and `startTime == endTime`. Both the
type code and the description are filtered, and the count of discarded rows is
reported. (Game `2026020005` contains 7 such rows.)

**Per-second state.** Each player's shifts are first collapsed into a set of
seconds, so duplicated or overlapping rows for one player cannot inflate a
team's skater count. Skaters and goalies are counted separately per team, with
goalie identity taken from the play-by-play roster (`positionCode == "G"`). A
second is clean 5v5 only when the state is exactly 5 skaters + 1 goalie for
**both** teams.

## Shift sources

`loader.load_game_context` prefers the JSON `shiftcharts` feed. When that
returns zero rows, it parses the HTML Time On Ice reports instead — `TV{num}`
for the visitor, `TH{num}` for the home side, where `num` is the last six
digits of the game id. Both feeds produce the same `Shift` records, so nothing
downstream knows which was used; the source is recorded per game and printed in
the validation summary. `--no-html-fallback` disables it.

The reports carry no player ids, so players are joined to the play-by-play
roster on **(team, sweater number)**, with the surname as a cross-check.

Two traps in that format, both of which produced wrong numbers before being
fixed:

- **Per-period summary rows look like shifts.** Each player's section ends with
  a summary table whose rows also lead with a digit and also contain `MM:SS`
  cells — `['1', '8', '00:45', '06:00', ...]`. Accepting a bare `MM:SS` adds one
  phantom shift per period, which inflated every player's TOI by hundreds of
  seconds. A shift row is identified by requiring the full
  `elapsed / remaining` pair, slash included, which no summary row has.
- **Surnames contain spaces.** Comparing the report's `DEL MASTRO, ETHAN`
  against the last whitespace token of a flat `"Ethan Del Mastro"` reports a
  mismatch that is not real. `Player.last_name` is kept as its own field so the
  comparison is exact.

## Columns

| Column | Meaning |
| --- | --- |
| `toi_5v5` | Minutes on ice at clean 5v5 |
| `gf_on` / `ga_on` / `gd_on` | Clean-5v5 goals for / against / differential while on ice |
| `official_pm` | The NHL's own +/-, all situations, over the same games |
| `nonclean_pm` | `official_pm - gd_on`: the part earned away from clean 5v5 |
| `share` | `toi_5v5 / team_toi_5v5` |
| `expected` | `team_gd_5v5 * share` |
| `adjusted` | `gd_on - expected` — sorts the table |
| `rel` | `gd_on - gd_off * (toi_5v5 / toi_off)` |
| `gd_on_per_60`, `team_gd_per_60` | Rate forms |

`Team_TOI_5v5` is elapsed clean-5v5 time, not a sum over players. `rel` is the
TOI-scaled differential as specified, not the conventional per-60 relative
stat, and is blank when a player has no off-ice time.

`nonclean_pm` collects everything official +/- counts that clean 5v5 does not:
empty-net goals at either end, 4v4 and 3v3 goals, shorthanded goals, and goals
conceded while on the power play. It is not noise — for several Blackhawks it is
the larger share of their published number.

`GP` counts games **in this calculation** — games with usable shift data in
which the skater took at least one shift. With the HTML fallback in place all
seven completed games are included, so `GP` currently equals season games
played for every skater; it would diverge again if a game were ever skipped.

## Validation

`main.py` prints, and `verify.py` independently re-checks:

- **The TOI identity.** Five skaters are on the ice for every clean-5v5 second
  by definition, so summed skater TOI must equal exactly `5 x` team elapsed
  time. Reported as OK/FAIL, not assumed.
- **Official TOI agreement.** `verify.py` compares per-player total on-ice time
  from the timeline against the `boxscore` feed's published `toi` — a feed the
  pipeline does not otherwise touch. Currently 266/266 player-games match to
  the exact second, across both shift sources.
- **Official +/- rebuilt from first principles.** `verify.py` reconstructs each
  skater's official +/- from play-by-play goals plus shift-derived on-ice sets
  and compares it to the boxscore. Currently exact for all 23 skaters, which
  validates `official_pm` and pins down what `nonclean_pm` contains.
- **Feed cross-checks.** `situationCode` versus the shift-derived on-ice state,
  in both directions; whether exactly 5 of the club's skaters were on ice for
  each counted goal; and whether the scorer the play-by-play credits is placed
  on the ice by the shift data.
- **Coverage.** Games are skipped, with the reason printed, when neither shift
  source yields rows, a period with play-by-play events has no shifts, a
  regulation period ends early, a team shows zero skaters for >=2% of the game,
  or clean 5v5 time is implausibly low.

### The plus-minus eligibility rule

`stats.counts_for_plus_minus` compares **total players per side (skaters +
goalie)**, not skater counts. A team that has pulled its goalie ices six
skaters without holding a man advantage, so a goal it scores does earn plus.
Testing skater counts instead classifies that as a power-play goal and drops a
real plus — it cost six Blackhawks a point each in the reconstruction before
being corrected.

## Data provenance note

As of 2026-10-13, `shiftcharts` returns zero rows for three of the seven
completed Blackhawks games (`2026020013`, `2026020022`, `2026020050`),
reproducibly and not as a transport error. Play-by-play for those games is
complete, so the gap is specific to that endpoint. All three are recovered from
the HTML reports and verified to the exact second against boxscore TOI, so no
game is currently excluded.


## The website

`update.py` publishes `docs/index.html`, a single self-contained page (no
external CSS, JS or fonts), served by GitHub Pages from `/docs`.

Order is the point: **fetch, then verify, then publish.** The page is written
only after `verify.py` exits clean, so a failed check leaves the previously
published page live and exits non-zero for CI to report. This was tested by
corrupting a cached boxscore: `update.py` exited 1 with `PUBLISH BLOCKED` and
the published page's hash was unchanged.

Fetching is incremental. `Fetcher` caches every response verbatim, so a run
reaches the network only for games it has never seen -- a no-op day costs one
request (the schedule). The cache runs about 0.5 MB per game, so a full
82-game season lands near 42 MB in the repo.

### Reading the table

Sortable on any column, defaulting to Adjusted descending. On a phone the
Player column is pinned left and Adjusted is pinned right, so the row label and
the headline number stay on screen while the columns between them scroll.

### A note on the colours

`Adjusted` is a polarity encoding, so it gets a diverging treatment: a signed
numeral in primary ink, a wash, and a bar that grows right for positive and
left for negative from a centre tick.

Green/red is the hockey convention and is what ships, but it is worth being
honest about it: measured with the palette validator, green `#0ca30c` against
red `#d03b3b` separates by **deutan Delta E 4.1**, below even the 6-8 floor
that secondary encoding can rescue. Roughly one man in twelve cannot tell those
two hues apart. The column is still readable for them because hue is never the
only cue -- the sign on the numeral and the direction of the bar both carry
polarity on their own.

To make the page safe on hue alone, change `ACCENT_POSITIVE` in
`nhlapm/config.py` to `#2a78d6`. Blue against the same red measures deutan
Delta E 23.8 and passes every check in both light and dark mode.

Dark mode is a selected set of steps against the dark surface rather than an
inversion, and the Adjusted washes are opaque (not alpha) because a sticky cell
must not be see-through while columns scroll underneath it.

## Automation

`.github/workflows/update.yml` runs daily at 11:00 UTC -- 06:00 Central on CDT,
05:00 once CST starts -- and from the **Run workflow** button. Steps: fetch and
compute, verify, build, then commit `cache/` and `docs/` back to the repo. The
job has no dependencies to install; it is standard library only.
