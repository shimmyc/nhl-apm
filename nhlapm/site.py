"""Render the standings page: one self-contained HTML file, no dependencies.

Design notes worth keeping:

* The form is a table plus a row of stat tiles, not a chart. Twenty-three rows
  of eleven per-player measures is exactly what a table is for, and the two
  team totals are single headline numbers.
* `Adjusted` is a polarity encoding, so it gets a diverging treatment: a signed
  numeral in primary ink plus a bar that grows right for positive and left for
  negative from a centre tick.
* The green/red pair is the hockey convention and is what ships, but it
  measures deutan Delta E 4.1 -- below the usable floor -- so hue is never the
  only cue. The sign on the numeral and the bar's direction both carry polarity
  without hue, which is what keeps the column readable for a colourblind
  reader. config.ACCENT_* swaps the pair.
* Numerals wear text tokens, never the accent colour, so contrast against the
  surface never depends on the palette.
* Dark mode is its own set of steps against the dark surface, not an inversion.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config
from .pipeline import TeamRun
from .stats import PlayerRow

# (key, header, short header, tooltip/legend, numeric?, decimals)
COLUMNS = [
    ("name", "Player", "Player", "", False, None),
    ("position", "Pos", "Pos", "", False, None),
    ("games_played", "GP", "GP", "Games included in this calculation", True, 0),
    ("toi_5v5", "TOI_5v5", "TOI", "Minutes on ice at 5v5 with both goalies in net", True, 1),
    ("official_pm", "Official_PM", "Off +/-", "The NHL's published plus-minus", True, 0),
    ("gd_on", "GD_on", "GD on", "5v5 goal differential while on the ice", True, 0),
    ("nonclean_pm", "NonClean", "NonCln", "Official minus 5v5: the part earned elsewhere", True, 0),
    ("expected", "Expected", "Exp", "What an average skater on this team would post in these minutes", True, 2),
    ("adjusted", "Adjusted", "Adj", "GD_on minus Expected", True, 2),
    ("rel", "Rel", "Rel", "On-ice differential against the same team without them", True, 2),
    ("gd_on_per_60", "GD_on/60", "GD/60", "5v5 goal differential per 60 minutes", True, 2),
]

SIGNED_KEYS = {"official_pm", "gd_on", "nonclean_pm", "expected", "adjusted",
               "rel", "gd_on_per_60"}

LEGEND = [
    ("TOI_5v5", "Minutes this skater was on the ice at 5-on-5 with both goalies in "
                "net. Power plays, 4-on-4, 3-on-3 overtime and empty-net time are all "
                "excluded."),
    ("Official_PM", "The plus-minus the NHL publishes. It counts every situation "
                    "except power-play goals, so empty-net goals and shorthanded goals "
                    "land in it."),
    ("GD_on", "Goals for minus goals against while this skater was on the ice, "
              "counting only clean 5-on-5 play."),
    ("NonClean", "Official_PM minus GD_on: how much of the published number came "
                 "from somewhere other than 5-on-5 - empty nets, 4-on-4, shorthanded "
                 "goals. A big number here means the official figure is telling you "
                 "about special teams, not about this skater's 5-on-5 play."),
    ("Expected", "The 5-on-5 goal differential an average skater on this team would "
                 "have posted in the same number of minutes. On a team being outscored "
                 "this is negative for everyone."),
    ("Adjusted", "GD_on minus Expected: 5-on-5 goal differential versus what an "
                 "average Hawk would have in the same minutes. Positive means the team "
                 "did better with them on the ice than it did on average."),
    ("Rel", "On-ice differential compared with the same team's differential while "
            "this skater was on the bench, scaled by the minutes split."),
    ("GD_on/60", "GD_on expressed as a rate per 60 minutes of 5-on-5, so short-TOI "
                 "skaters are comparable with regulars - though a small number of "
                 "minutes makes the rate jumpy."),
]


@dataclass
class SiteData:
    runs: list[TeamRun]
    generated_at_utc: datetime


def central_time(moment: datetime) -> tuple[datetime, str]:
    """(local time, abbreviation) for US Central.

    Prefers the IANA database; falls back to the US DST rules so the build does
    not depend on tzdata being present (Windows ships none by default).
    """
    try:
        from zoneinfo import ZoneInfo
        local = moment.astimezone(ZoneInfo(config.DISPLAY_TIMEZONE))
        return local, local.strftime("%Z") or "CT"
    except Exception:
        naive = moment.astimezone(timezone.utc).replace(tzinfo=None)

        def nth_sunday(month: int, nth: int) -> datetime:
            first = datetime(naive.year, month, 1)
            offset = (6 - first.weekday()) % 7
            return first + timedelta(days=offset + 7 * (nth - 1))

        # 2am local changeovers, expressed in UTC.
        dst_start = nth_sunday(3, 2) + timedelta(hours=8)
        dst_end = nth_sunday(11, 1) + timedelta(hours=7)
        in_dst = dst_start <= naive < dst_end
        shift, label = (-5, "CDT") if in_dst else (-6, "CST")
        return (moment.astimezone(timezone.utc) + timedelta(hours=shift)).replace(
            tzinfo=None), label


def _format(row: PlayerRow, key: str, decimals: int | None) -> str:
    value = getattr(row, key)
    if value is None:
        return "--"
    if decimals is None:
        return str(value)
    if key in SIGNED_KEYS:
        # A value that rounds to zero prints unsigned. Without this a tiny
        # negative float (a skater sitting exactly at expectation) renders as
        # "-0.00" and gets washed red for no reason.
        if round(value, decimals) == 0:
            return f"{0:.{decimals}f}" if decimals else "0"
        return f"{value:+.{decimals}f}" if decimals else f"{value:+d}"
    return f"{value:.{decimals}f}" if decimals else str(value)


def _sort_value(row: PlayerRow, key: str) -> str:
    value = getattr(row, key)
    if value is None:
        # Sorts last in either direction rather than pretending to be zero.
        return ""
    return f"{value}"


def _table(run: TeamRun) -> str:
    rows = run.rows
    peak = max((abs(r.adjusted) for r in rows), default=1.0) or 1.0

    head_cells = []
    for index, (key, header, short, tip, numeric, _dec) in enumerate(COLUMNS):
        sort = ' aria-sort="descending"' if key == "adjusted" else ' aria-sort="none"'
        classes = "num" if numeric else "txt"
        title = f' title="{html.escape(tip)}"' if tip else ""
        head_cells.append(
            f'<th scope="col" class="{classes}" data-col="{index}" '
            f'data-type="{"num" if numeric else "txt"}"{sort}{title}>'
            f'<button type="button"><span class="full">{html.escape(header)}</span>'
            f'<span class="abbr">{html.escape(short)}</span>'
            f'<span class="arrow" aria-hidden="true"></span></button></th>')

    body_rows = []
    for row in rows:
        cells = []
        for key, _header, _short, _tip, numeric, decimals in COLUMNS:
            text = _format(row, key, decimals)
            sort_attr = f' data-v="{html.escape(_sort_value(row, key))}"'
            if key == "name":
                cells.append(f'<th scope="row" class="txt player"{sort_attr}>'
                             f'{html.escape(text)}</th>')
            elif key == "adjusted":
                shown = round(row.adjusted, decimals or 2)
                width = min(100.0, abs(row.adjusted) / peak * 100.0)
                # Derived from the printed value, so the wash can never
                # disagree with the number beside it.
                wash = "z" if shown == 0 else ("p" if shown > 0 else "n")
                side = "pos" if shown >= 0 else "neg"
                bar = ("" if shown == 0 else
                       f'<i class="{side}" style="width:{width / 2:.1f}%"></i>')
                cells.append(
                    f'<td class="num adj {wash}"{sort_attr}>'
                    f'<span class="adjnum">{html.escape(text)}</span>'
                    f'<span class="bar" aria-hidden="true">{bar}</span></td>')
            else:
                classes = "num" if numeric else "txt"
                cells.append(f'<td class="{classes}"{sort_attr}>{html.escape(text)}</td>')
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div class="tablewrap">\n<table id="t-' + html.escape(run.team) + '">\n'
        '<caption class="sr-only">'
        f'{html.escape(run.team)} skaters, 5-on-5 adjusted plus-minus, sortable'
        '</caption>\n<thead><tr>' + "".join(head_cells) + '</tr></thead>\n<tbody>\n'
        + "\n".join(body_rows) + '\n</tbody>\n</table>\n</div>'
    )


def _tiles(run: TeamRun) -> str:
    stats = run.stats
    minutes = stats.team_toi_seconds / 60.0
    per60 = (stats.team_gd * 3600.0 / stats.team_toi_seconds
             if stats.team_toi_seconds else 0.0)
    tiles = [
        ("5v5 goals for", f"{stats.team_gf}", ""),
        ("5v5 goals against", f"{stats.team_ga}", ""),
        ("5v5 differential", f"{stats.team_gd:+d}", f"{per60:+.2f} per 60"),
        ("5v5 minutes", f"{minutes:,.0f}", f"{len(stats.games)} games"),
    ]
    out = []
    for label, value, sub in tiles:
        sub_html = f'<span class="tsub">{html.escape(sub)}</span>' if sub else ""
        out.append(f'<div class="tile"><span class="tlabel">{html.escape(label)}</span>'
                   f'<span class="tval">{html.escape(value)}</span>{sub_html}</div>')
    return '<div class="tiles">' + "".join(out) + "</div>"


def _provenance(run: TeamRun) -> str:
    sources: dict[str, int] = {}
    for game in run.stats.games:
        sources[game.source] = sources.get(game.source, 0) + 1
    parts = [f"{count} from {html.escape(name)}" for name, count in sorted(sources.items())]
    line = f"Shift data: {', '.join(parts)}." if parts else ""
    if run.skipped:
        line += (f" {len(run.skipped)} game(s) excluded for incomplete shift data, "
                 f"so GP counts games in this calculation rather than season games played.")
    else:
        line += " No games excluded."
    return line


def render_page(data: SiteData) -> str:
    local, tz_label = central_time(data.generated_at_utc)
    # Built piecewise on purpose: the %-d / %-I no-pad forms are glibc
    # extensions and raise on Windows, so the build would break locally.
    stamp = (f"{local.strftime('%a %b')} {local.day}, {local.year} at "
             f"{local.strftime('%I:%M %p').lstrip('0')} {tz_label}")

    runs = [run for run in data.runs if run.rows]
    primary = runs[0] if runs else None
    season = primary.season if primary else config.SEASON
    season_label = f"{season[:4]}-{season[6:]}" if len(season) == 8 else season

    sections = []
    for run in runs:
        sections.append(
            f'<section class="team" id="team-{html.escape(run.team)}">\n'
            f'<h2>{html.escape(run.team)} skaters</h2>\n'
            f'{_tiles(run)}\n{_table(run)}\n'
            f'<p class="hint">Player and Adjusted stay pinned. Swipe the table '
            f'sideways for the columns in between.</p>\n'
            f'<p class="prov">{_provenance(run)}</p>\n</section>')

    legend_items = "".join(
        f"<div class='lrow'><dt>{html.escape(term)}</dt><dd>{html.escape(text)}</dd></div>"
        for term, text in LEGEND)

    if not runs:
        sections = ['<section class="team"><p class="prov">No completed games with '
                    'usable shift data yet this season.</p></section>']

    title = config.SITE_TITLE
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="5-on-5 adjusted plus-minus for Chicago Blackhawks skaters, rebuilt daily from NHL play-by-play and shift data.">
<meta name="color-scheme" content="light dark">
<style>
:root {{
  --page: #f9f9f7;
  --surface: #fcfcfb;
  --ink: #0b0b0b;
  --ink-2: #52514e;
  --ink-3: #78776f;
  --rule: #e4e3de;
  --rule-2: #d3d2cb;
  --track: #ecebe6;
  --pos: {config.ACCENT_POSITIVE};
  --neg: {config.ACCENT_NEGATIVE};
  --pos-wash: #e4f3e3;
  --neg-wash: #f8e9e8;
  --focus: #2a78d6;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --page: #0d0d0d;
    --surface: #1a1a19;
    --ink: #f0efec;
    --ink-2: #c3c2b7;
    --ink-3: #96958c;
    --rule: #2c2c2a;
    --rule-2: #3a3a37;
    --track: #2c2c2a;
    --pos: #0ca30c;
    --neg: #e05555;
    --pos-wash: #183017;
    --neg-wash: #362221;
    --focus: #3987e5;
  }}
}}
:root[data-theme="dark"] {{
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
  --ink-3: #96958c; --rule: #2c2c2a; --rule-2: #3a3a37; --track: #2c2c2a;
  --pos: #0ca30c; --neg: #e05555;
  --pos-wash: #183017; --neg-wash: #362221; --focus: #3987e5;
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font: 400 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums;
  padding: 0 16px 56px;
}}
.wrap {{ max-width: 1040px; margin: 0 auto; }}
header {{ padding: 28px 0 8px; }}
h1 {{ font-size: 1.4rem; line-height: 1.25; margin: 0 0 4px; letter-spacing: -.01em; }}
.sub {{ color: var(--ink-2); font-size: .85rem; margin: 0; }}
.stamp {{ color: var(--ink-3); font-size: .78rem; margin: 6px 0 0; }}
h2 {{ font-size: 1rem; margin: 28px 0 10px; color: var(--ink-2); font-weight: 600;
      text-transform: uppercase; letter-spacing: .06em; }}
.tiles {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 0 0 14px; }}
@media (min-width: 620px) {{ .tiles {{ grid-template-columns: repeat(4, 1fr); }} }}
.tile {{ background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
         padding: 10px 12px; display: flex; flex-direction: column; gap: 1px; }}
.tlabel {{ font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-3); }}
.tval {{ font-size: 1.45rem; font-weight: 650; line-height: 1.1; }}
.tsub {{ font-size: .72rem; color: var(--ink-3); }}
.tablewrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--rule);
              border-radius: 10px; -webkit-overflow-scrolling: touch; }}
table {{ border-collapse: separate; border-spacing: 0; width: 100%; font-size: .82rem; }}
th, td {{ padding: 7px 9px; white-space: nowrap; border-bottom: 1px solid var(--rule); }}
tbody tr:last-child th, tbody tr:last-child td {{ border-bottom: 0; }}
thead th {{ position: sticky; top: 0; z-index: 3; background: var(--surface);
            border-bottom: 1px solid var(--rule-2); padding: 0; }}
/* width:auto + min-width:100% so the header's own text sets the column's
   minimum. With width:100% the button contributes no intrinsic width, the
   column sizes to the data, and the next sticky th's opaque background paints
   over the overflow -- headers look truncated. */
thead th button {{ all: unset; box-sizing: border-box; display: block;
  width: auto; min-width: 100%; cursor: pointer;
  padding: 9px; font: inherit; font-size: .68rem; font-weight: 650; color: var(--ink-2);
  text-transform: uppercase; letter-spacing: .04em; }}
thead th.num button {{ text-align: right; }}
thead th button:focus-visible {{ outline: 2px solid var(--focus); outline-offset: -2px; }}
thead th[aria-sort="none"] .arrow::after {{ content: ""; }}
thead th[aria-sort="descending"] .arrow::after {{ content: " \\2193"; }}
thead th[aria-sort="ascending"] .arrow::after {{ content: " \\2191"; }}
thead th:not([aria-sort="none"]) button {{ color: var(--ink); }}
.num {{ text-align: right; }}
.txt {{ text-align: left; }}
th.player, td:first-child {{ position: sticky; left: 0; z-index: 2;
  background: var(--surface); font-weight: 500; }}
thead th:first-child {{ z-index: 4; }}
th.player {{ border-right: 1px solid var(--rule); }}
thead th:first-child {{ border-right: 1px solid var(--rule); }}
tbody tr:nth-child(even) th.player, tbody tr:nth-child(even) td {{ background: var(--page); }}
tbody tr:nth-child(even) th.player {{ background: var(--page); }}
.abbr {{ display: none; }}
td.adj {{ min-width: 92px; }}
.adjnum {{ display: block; font-weight: 650; }}
.bar {{ position: relative; display: block; height: 4px; margin: 3px 0 0 auto;
        width: 100%; max-width: 76px; background: var(--track); border-radius: 2px; }}
.bar::before {{ content: ""; position: absolute; left: 50%; top: -2px; width: 1px;
                height: 8px; background: var(--rule-2); }}
.bar i {{ position: absolute; top: 0; height: 4px; border-radius: 2px; min-width: 2px; }}
.bar i.pos {{ left: 50%; background: var(--pos); }}
.bar i.neg {{ right: 50%; background: var(--neg); }}
td.adj.p {{ background: var(--pos-wash); }}
td.adj.n {{ background: var(--neg-wash); }}
td.adj.z {{ background: var(--surface); }}
tbody tr:nth-child(even) td.adj.z {{ background: var(--page); }}
.hint {{ display: none; color: var(--ink-3); font-size: .72rem; margin: 8px 2px 0; }}
@media (max-width: 760px) {{
  .full {{ display: none; }}
  .abbr {{ display: inline; }}
  .hint {{ display: block; }}
  table {{ font-size: .76rem; }}
  th, td {{ padding: 6px 7px; }}
  thead th button {{ padding: 8px 7px; font-size: .64rem; }}
  th.player {{ max-width: 112px; overflow: hidden; text-overflow: ellipsis; }}
  td.adj {{ min-width: 72px; }}
  .bar {{ max-width: 54px; }}
  /* Player pinned left, Adjusted pinned right: the row label and the headline
     number stay on screen while the middle columns scroll between them. */
  td.adj, thead th[data-col="8"] {{ position: sticky; right: 0; z-index: 2;
    border-left: 1px solid var(--rule-2); }}
  thead th[data-col="8"] {{ z-index: 4; background: var(--surface); }}
  .tval {{ font-size: 1.3rem; }}
}}
.prov {{ color: var(--ink-3); font-size: .76rem; margin: 10px 2px 0; }}
details.legend {{ margin: 26px 0 0; background: var(--surface);
  border: 1px solid var(--rule); border-radius: 10px; padding: 0 14px; }}
details.legend summary {{ cursor: pointer; padding: 12px 0; font-weight: 600;
  font-size: .9rem; }}
details.legend summary:focus-visible {{ outline: 2px solid var(--focus); outline-offset: 2px; }}
dl {{ margin: 0 0 12px; }}
.lrow {{ padding: 9px 0; border-top: 1px solid var(--rule); }}
dt {{ font-weight: 650; font-size: .82rem; margin: 0 0 2px; }}
dd {{ margin: 0; color: var(--ink-2); font-size: .84rem; }}
footer {{ margin-top: 26px; color: var(--ink-3); font-size: .74rem; }}
footer a {{ color: var(--ink-2); }}
.sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{html.escape(title)}</h1>
  <p class="sub">5-on-5 only, both goalies in net &middot; {html.escape(season_label)} regular season</p>
  <p class="stamp">Updated {html.escape(stamp)}</p>
</header>
{"".join(sections)}

<details class="legend">
  <summary>What the columns mean</summary>
  <dl>{legend_items}</dl>
</details>

<footer>
  <p>Built from the NHL's public play-by-play, shift-chart and boxscore feeds.
  Only goals with <code>situationCode 1551</code> (five skaters a side, both
  goalies in net) count toward GD_on. Tap a column heading to sort.</p>
  <p>Small samples move these numbers a lot early in a season: a single goal
  can swing Adjusted by more than a point.</p>
  <p><a href="hawks_adjusted_pm.csv">Download the full CSV</a> (every column,
  including the ones not shown here).</p>
</footer>
</div>
<script>
(function () {{
  document.querySelectorAll("table").forEach(function (table) {{
    var body = table.tBodies[0];
    if (!body) return;
    var heads = Array.prototype.slice.call(table.tHead.rows[0].cells);

    heads.forEach(function (th, index) {{
      var button = th.querySelector("button");
      if (!button) return;
      button.addEventListener("click", function () {{
        var numeric = th.dataset.type === "num";
        var current = th.getAttribute("aria-sort");
        // First click on a new column: numbers open descending, names ascending.
        var descending = current === "none" ? numeric : current !== "descending";

        heads.forEach(function (other) {{ other.setAttribute("aria-sort", "none"); }});
        th.setAttribute("aria-sort", descending ? "descending" : "ascending");

        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {{
          var x = a.cells[index].getAttribute("data-v");
          var y = b.cells[index].getAttribute("data-v");
          // Blanks ("--" values) always sort to the bottom.
          var xe = x === "" || x === null, ye = y === "" || y === null;
          if (xe || ye) return xe && ye ? 0 : (xe ? 1 : -1);
          if (numeric) {{
            var d = parseFloat(x) - parseFloat(y);
            return descending ? -d : d;
          }}
          var c = String(x).localeCompare(String(y));
          return descending ? -c : c;
        }});
        rows.forEach(function (row) {{ body.appendChild(row); }});
      }});
    }});
  }});
}})();
</script>
</body>
</html>
"""
