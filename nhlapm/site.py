"""Render docs/index.html: a static shell that filters and aggregates in the browser.

The page carries no numbers of its own. It loads docs/data/index.json (the
season manifest), then that season's file and players.json, and folds the
player-game rows the filters select -- the same sums as aggregate.py. One
download, then every filter is a re-sum in memory: no request per click.

A season range or a whole career folds docs/data/player_seasons.json instead
(one row per player-season-club, see careers.py), so a phone never downloads
19 game-level files. Game-level filters (last N, dates, venue, rookies) apply
only within a single season and are hidden otherwise.

docs/player.html is a second static shell: one skater's season-by-season
table, career totals and an Official_PM vs Rel chart, from the same file.

Design notes worth keeping:

* `Rel` is the headline stat: it sits beside the name, is the default sort,
  and is the polarity encoding that gets a diverging treatment -- a signed
  numeral in primary ink plus a bar that grows right for positive and left for
  negative from a centre tick. Adjusted stays as a secondary column.
* The green/red pair is the hockey convention and is what ships, but it
  measures deutan Delta E 4.1 -- below the usable floor -- so hue is never the
  only cue. The sign on the numeral and the bar's direction both carry
  polarity without hue. config.ACCENT_* swaps the pair.
* Numerals wear text tokens, never the accent colour, so contrast against the
  surface never depends on the palette.
* Dark mode is its own set of steps against the dark surface, not an inversion,
  and the Adjusted washes are opaque because a sticky cell must not be
  see-through while columns scroll underneath it.
* Min TOI has no fixed default: it follows the view (0 for one team, 50
  otherwise) until the reader types a value, and only a typed value goes in
  the URL.
* ~1,000 skaters on a phone: rows render as one string into innerHTML, capped
  at 100 until "Show all" is pressed, and the name search is debounced.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import config
from .store import Season, season_label

DEFAULT_MIN_TOI = 50        # league / conference / division views, one season
DEFAULT_MIN_TOI_TEAM = 0    # one team: show the whole roster
DEFAULT_MIN_TOI_CAREER = 500  # a season range scales 50 per season up to this

LEGEND = [
    ("Seasons", "One season (the default is the current one), a range of seasons, or "
                "each skater's entire career in the data (2007-08 on). Ranges and careers "
                "are built from season totals per club, so the games, venue and rookie "
                "filters only apply within a single season."),
    ("Scope", "League, a conference, a division or one team. A skater traded "
              "mid-season shows as one combined row (\"CHI/NYR\") league-wide and "
              "as a separate row for each club when you pick that club."),
    ("Games", "Season to date, each skater's last 5, 10 or 20 games within the "
              "other filters, or a custom date range."),
    ("Rel", "5v5 goal differential vs. how his team does when he's on the bench, "
            "scaled to his minutes. The headline number and the default sort: "
            "Rel = GD_on - GD_off x (TOI_on / TOI_off), with the off-ice figures "
            "taken from the same games he played. For a traded skater, a season "
            "range or a career, Rel is worked out per club per season and added up."),
    ("Min TOI", "Hides skaters below this many 5-on-5 minutes. It defaults to 0 when "
                "one team is picked, so the whole roster shows; otherwise 50 for one "
                "season, 50 per season for a range (up to 500), and 500 for careers."),
    ("TOI_5v5", "Minutes on the ice at 5-on-5 with both goalies in net. Power "
                "plays, 4-on-4, 3-on-3 overtime and empty-net time are all excluded."),
    ("Official_PM", "The plus-minus the NHL publishes, over the same games. It "
                    "counts every situation except power-play goals, so empty-net "
                    "and shorthanded goals land in it."),
    ("GD_on", "Goals for minus goals against while the skater was on the ice, "
              "counting only clean 5-on-5 play."),
    ("NonClean", "Official_PM minus GD_on: the part of the published number earned "
                 "away from 5-on-5 - empty nets, 4-on-4, shorthanded goals."),
    ("Expected", "Built game by game: the team's 5-on-5 goal differential in each "
                 "game times the skater's share of that game's 5-on-5 minutes, "
                 "summed. On a team being outscored it is negative for everyone."),
    ("Adjusted", "GD_on minus Expected: how the team did with this skater on the "
                 "ice compared with how it did overall in the same games. Every "
                 "team's Adjusted sums to zero in every game."),
    ("GD_on/60", "GD_on per 60 minutes of 5-on-5. Rates are jumpy on small samples, "
                 "which is what the minimum-TOI filter is for."),
    ("Per 60", "Switches GD_on, Expected, Adjusted and Rel to rates per 60 minutes "
               "of 5-on-5, so a third-pair defenceman and a top-line centre compare "
               "on equal footing. Official_PM and NonClean stay as totals."),
]


@dataclass
class SiteData:
    season: Season
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

        dst_start = nth_sunday(3, 2) + timedelta(hours=8)
        dst_end = nth_sunday(11, 1) + timedelta(hours=7)
        in_dst = dst_start <= naive < dst_end
        shift, label = (-5, "CDT") if in_dst else (-6, "CST")
        return (moment.astimezone(timezone.utc) + timedelta(hours=shift)).replace(
            tzinfo=None), label


def render_page(data: SiteData) -> str:
    local, tz_label = central_time(data.generated_at_utc)
    # Built piecewise on purpose: %-d / %-I are glibc-only and raise on Windows.
    stamp = (f"{local.strftime('%a %b')} {local.day}, {local.year} at "
             f"{local.strftime('%I:%M %p').lstrip('0')} {tz_label}")
    legend = "".join(
        f"<div class='lrow'><dt>{html.escape(term)}</dt><dd>{html.escape(text)}</dd></div>"
        for term, text in LEGEND)
    replacements = {
        "__TITLE__": html.escape(config.SITE_TITLE),
        "__SEASON_LABEL__": html.escape(season_label(data.season.season)),
        "__STAMP__": html.escape(stamp),
        "__LEGEND__": legend,
        "__POS__": config.ACCENT_POSITIVE,
        "__NEG__": config.ACCENT_NEGATIVE,
        "__DEFAULT_MIN__": json.dumps(DEFAULT_MIN_TOI),
        "__DEFAULT_MIN_TEAM__": json.dumps(DEFAULT_MIN_TOI_TEAM),
        "__DEFAULT_MIN_CAREER__": json.dumps(DEFAULT_MIN_TOI_CAREER),
        "__DEFAULT_TEAM__": json.dumps(config.DEFAULT_PAGE_TEAM),
    }
    page = TEMPLATE
    for key, value in replacements.items():
        page = page.replace(key, value)
    return page


def render_player_page() -> str:
    """docs/player.html: one skater's seasons, from data/player_seasons.json."""
    page = PLAYER_TEMPLATE
    for key, value in {"__TITLE__": html.escape(config.SITE_TITLE),
                       "__POS__": config.ACCENT_POSITIVE,
                       "__NEG__": config.ACCENT_NEGATIVE}.items():
        page = page.replace(key, value)
    return page


PLAYER_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Player Seasons</title>
<meta name="description" content="One NHL skater's 5-on-5 Rel and official plus-minus, season by season.">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cellipse cx='16' cy='18' rx='13' ry='6' fill='%23111'/%3E%3Cellipse cx='16' cy='14' rx='13' ry='6' fill='%23333'/%3E%3C/svg%3E">
<style>
:root {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --ink-3: #78776f; --rule: #e4e3de; --rule-2: #d3d2cb; --track: #ecebe6;
  --pos: __POS__; --neg: __NEG__; --focus: #2a78d6; --rel: #2a78d6; --pm: #78776f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
    --ink-3: #96958c; --rule: #2c2c2a; --rule-2: #3a3a37; --track: #2c2c2a;
    --pos: #0ca30c; --neg: #e05555; --focus: #3987e5; --rel: #5a9ff0; --pm: #a3a296;
  }
}
:root[data-theme="dark"] {
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
  --ink-3: #96958c; --rule: #2c2c2a; --rule-2: #3a3a37; --track: #2c2c2a;
  --pos: #0ca30c; --neg: #e05555; --focus: #3987e5; --rel: #5a9ff0; --pm: #a3a296;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 400 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums; padding: 0 16px 56px; }
.wrap { max-width: 1000px; margin: 0 auto; }
header { padding: 22px 0 6px; }
.back { font-size: .82rem; color: var(--ink-2); }
h1 { font-size: 1.5rem; line-height: 1.2; margin: 8px 0 2px; letter-spacing: -.01em; }
.sub { color: var(--ink-2); font-size: .86rem; margin: 0; }
.tiles { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 14px 0; }
@media (min-width: 620px) { .tiles { grid-template-columns: repeat(5, 1fr); } }
.tile { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  padding: 10px 12px; display: flex; flex-direction: column; min-width: 0; }
.tlabel { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-3); }
.tval { font-size: 1.4rem; font-weight: 650; line-height: 1.15; }
.card { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; padding: 12px 12px 6px; margin: 0 0 14px; }
.card h2 { font-size: .9rem; margin: 0 0 2px; }
.card p.note { color: var(--ink-3); font-size: .76rem; margin: 0 0 6px; }
.key { display: flex; gap: 16px; font-size: .78rem; color: var(--ink-2); margin: 4px 0 2px; flex-wrap: wrap; }
.key svg { vertical-align: middle; margin-right: 4px; }
svg.chart { width: 100%; height: auto; display: block; overflow: visible; }
.chart text { font-size: 11px; fill: var(--ink-3); }
.chart .tm { font-size: 10px; fill: var(--ink-2); }
.chart .zero { stroke: var(--rule-2); }
.chart .grid { stroke: var(--rule); }
.chart .rel { stroke: var(--rel); fill: none; stroke-width: 2.5; }
.chart .pm { stroke: var(--pm); fill: none; stroke-width: 2; stroke-dasharray: 5 4; }
.chart circle.rel { fill: var(--rel); stroke: var(--surface); stroke-width: 1.5; }
.chart rect.pm { fill: var(--surface); stroke: var(--pm); stroke-width: 2; stroke-dasharray: none; }
.tablewrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--rule); border-radius: 10px; }
table { border-collapse: collapse; width: 100%; font-size: .82rem; }
th, td { padding: 7px 9px; white-space: nowrap; border-bottom: 1px solid var(--rule); text-align: right; }
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align: left; }
thead th { font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-2); font-weight: 650; }
tr.stint td { color: var(--ink-2); }
tr.stint td:first-child { padding-left: 20px; }
tr.total td { font-weight: 650; }
tr.career td { font-weight: 700; border-top: 2px solid var(--rule-2); border-bottom: 0; }
td.rel { font-weight: 650; }
.state { padding: 22px 0; color: var(--ink-3); }
footer { margin-top: 20px; color: var(--ink-3); font-size: .74rem; }
footer a, .back { color: var(--ink-2); }
a:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <a class="back" href="index.html">&larr; All skaters</a>
  <h1 id="name">Loading&hellip;</h1>
  <p class="sub" id="bio"></p>
</header>
<div id="main">
  <div class="tiles" id="tiles"></div>
  <section class="card" aria-labelledby="charth">
    <h2 id="charth">Official plus-minus vs Rel, by season</h2>
    <p class="note">Official_PM is the NHL's number, all situations. Rel is 5-on-5 goal differential vs. how his team did with him on the bench, scaled to his minutes.</p>
    <div class="key"><span><svg width="26" height="10" aria-hidden="true"><line x1="0" y1="5" x2="26" y2="5" stroke="var(--rel)" stroke-width="2.5"/><circle cx="13" cy="5" r="3.5" fill="var(--rel)"/></svg>Rel</span>
      <span><svg width="26" height="10" aria-hidden="true"><line x1="0" y1="5" x2="26" y2="5" stroke="var(--pm)" stroke-width="2" stroke-dasharray="5 4"/><rect x="9.5" y="1.5" width="7" height="7" fill="var(--surface)" stroke="var(--pm)" stroke-width="2"/></svg>Official_PM</span></div>
    <div id="chart"></div>
  </section>
  <div class="tablewrap">
  <table>
    <caption class="sr-only" style="position:absolute;left:-9999px">Season by season</caption>
    <thead><tr><th scope="col">Season</th><th scope="col">Team</th><th scope="col">GP</th><th scope="col">TOI_5v5</th>
      <th scope="col">Official_PM</th><th scope="col">GD_on</th><th scope="col">Rel</th><th scope="col">Adjusted</th></tr></thead>
    <tbody id="rows"><tr><td colspan="8" class="state">Loading&hellip;</td></tr></tbody>
  </table>
  </div>
  <p class="prov" id="partnote"></p>
</div>
<footer>
  <p>Every season in the data that passed verification. A traded season shows each club, then the season total.
  Rel for a traded season or a career is worked out per club per season and added up. Games that failed
  verification are left out of their season, so GP can be a game or two below the official count.</p>
  <p><a id="careerlink" href="index.html?career=1">Career leaderboard</a> &middot; <a href="data/player_seasons.json">Raw data</a></p>
</footer>
</div>
<script>
(function () {
"use strict";
var $ = function (id) { return document.getElementById(id); };
function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
function signed(v, d) { var r = Number(v.toFixed(d)); if (r === 0) return (0).toFixed(d); return (r > 0 ? "+" : "") + r.toFixed(d); }
var PART = {};
function label(id) { return id.slice(0, 4) + "-" + id.slice(6) + (PART[id] ? "†" : ""); }
var POS = {C: "Centre", L: "Left wing", R: "Right wing", D: "Defence"};
function getJson(url) { return fetch(url).then(function (r) { if (!r.ok) throw new Error(url + ": HTTP " + r.status); return r.json(); }); }
function rel(gd, goff, toi, toff) { return toff ? gd - goff * (toi / toff) : null; }

var pid = parseInt(new URLSearchParams(location.search).get("id"), 10);
var view = {seasons: []};
window.apmPlayer = function () { return view; };

getJson("data/index.json").then(function (m) {
  m.seasons.forEach(function (s) { if (s.quarantined) PART[s.id] = s; });
  var v = "?v=" + encodeURIComponent((m.seasons[0] || {}).updated || "");
  return Promise.all([getJson("data/" + (m.careers || "player_seasons.json") + v), getJson("data/" + m.players + v)]);
}).then(function (res) {
  var ps = res[0], pl = res[1], meta = pl.players[String(pid)];
  var mine = ps.rows.filter(function (r) { return r[1] === pid; });
  if (!pid || !mine.length) { $("name").textContent = "Player not found"; $("main").innerHTML = '<p class="state">No seasons for this player id in the data.</p>'; return; }
  var name = meta ? meta[0] : "Player " + pid;
  document.title = name + " | Player Seasons";
  $("name").textContent = name;
  var bits = [];
  if (meta && POS[meta[1]]) bits.push(POS[meta[1]]);
  if (meta && meta[2]) bits.push("shoots " + meta[2]);
  if (meta && meta[3]) bits.push("born " + meta[3]);
  $("bio").textContent = bits.join(" · ");
  $("careerlink").href = "index.html?career=1&q=" + encodeURIComponent(name) + "&min=0";

  // cols: s, pid, tm, gp, toi, gf, ga, pm, xm, toff, goff, rk
  var bySeason = {};
  mine.forEach(function (r) { (bySeason[r[0]] = bySeason[r[0]] || []).push(r); });
  var career = {gp: 0, toi: 0, gd: 0, pm: 0, xm: 0, rel: 0, n: 0};
  var html = [];
  Object.keys(bySeason).map(Number).sort(function (a, b) { return a - b; }).forEach(function (si) {
    var stints = bySeason[si], sid = ps.seasons[si];
    var t = {gp: 0, toi: 0, gd: 0, pm: 0, xm: 0, rel: 0, any: false, teams: []};
    stints.forEach(function (r) {
      var gd = r[5] - r[6], rv = rel(gd, r[10], r[4], r[9]);
      t.gp += r[3]; t.toi += r[4]; t.gd += gd; t.pm += r[7]; t.xm += r[8];
      if (rv !== null) { t.rel += rv; t.any = true; }
      t.teams.push(r[2]);
      r.relv = rv;
    });
    var row = function (cls, seasonCell, team, gp, toi, pm, gd, rv, adj) {
      return '<tr class="' + cls + '"><td>' + seasonCell + "</td><td>" + esc(team) + "</td><td>" + gp + "</td><td>" + (toi / 60).toFixed(1) +
        "</td><td>" + signed(pm, 0) + "</td><td>" + signed(gd, 0) + '</td><td class="rel">' + (rv === null ? "--" : signed(rv, 2)) +
        "</td><td>" + signed(adj, 2) + "</td></tr>";
    };
    if (stints.length > 1) {
      stints.forEach(function (r) { html.push(row("stint", "", r[2], r[3], r[4], r[7], r[5] - r[6], r.relv, (r[5] - r[6]) - r[8] / 10000)); });
      html.push(row("total", label(sid), t.teams.join("/"), t.gp, t.toi, t.pm, t.gd, t.any ? t.rel : null, t.gd - t.xm / 10000));
    } else {
      var r = stints[0];
      html.push(row("", label(sid), r[2], r[3], r[4], r[7], r[5] - r[6], r.relv, (r[5] - r[6]) - r[8] / 10000));
    }
    view.seasons.push({season: sid, team: t.teams.join("/"), gp: t.gp, toi: t.toi / 60, pm: t.pm, gd: t.gd,
                       rel: t.any ? t.rel : null, adj: t.gd - t.xm / 10000});
    career.gp += t.gp; career.toi += t.toi; career.gd += t.gd; career.pm += t.pm; career.xm += t.xm; career.rel += t.rel; career.n++;
  });
  html.push('<tr class="career"><td>Career</td><td>' + career.n + " season" + (career.n === 1 ? "" : "s") + "</td><td>" + career.gp + "</td><td>" +
    (career.toi / 60).toFixed(1) + "</td><td>" + signed(career.pm, 0) + "</td><td>" + signed(career.gd, 0) + '</td><td class="rel">' +
    signed(career.rel, 2) + "</td><td>" + signed(career.gd - career.xm / 10000, 2) + "</td></tr>");
  $("rows").innerHTML = html.join("");
  var partial = view.seasons.filter(function (x) { return PART[x.season]; });
  $("partnote").innerHTML = partial.length
    ? "† Partial season: games that failed verification are left out. " + partial.map(function (x) {
        var e = PART[x.season];
        return esc(label(x.season).replace("†", "") + ": " + e.games.toLocaleString() + " of " +
          (e.games + e.quarantined).toLocaleString() + " games verified");
      }).join("; ") + "." : "";
  view.partial = partial.map(function (x) { return x.season; });
  view.career = {gp: career.gp, toi: career.toi / 60, pm: career.pm, gd: career.gd, rel: career.rel, adj: career.gd - career.xm / 10000};
  var tile = function (l, v) { return '<div class="tile"><span class="tlabel">' + esc(l) + '</span><span class="tval">' + esc(v) + "</span></div>"; };
  $("tiles").innerHTML = tile("Seasons", String(career.n)) + tile("Games", String(career.gp)) +
    tile("Career Rel", signed(career.rel, 2)) + tile("Career Official_PM", signed(career.pm, 0)) +
    tile("Career Adjusted", signed(career.gd - career.xm / 10000, 2));
  drawChart(view.seasons);
}).catch(function (err) {
  $("name").textContent = "Could not load the data";
  $("main").innerHTML = '<p class="state">' + esc(err && err.message || err) + "</p>";
});

function drawChart(rows) {
  // Drawn at the container's real width, so 11px labels stay 11px on a phone.
  var W = Math.max(300, Math.round($("chart").clientWidth || 760)), H = W < 500 ? 240 : 300, L = 36, R = 12, T = 14, B = 46;
  var vals = [];
  rows.forEach(function (r) { vals.push(r.pm); if (r.rel !== null) vals.push(r.rel); });
  var lo = Math.min(0, Math.min.apply(null, vals)), hi = Math.max(0, Math.max.apply(null, vals));
  var step = Math.max(5, Math.ceil((hi - lo) / 6 / 5) * 5);
  lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  if (hi === lo) hi = lo + step;
  var n = rows.length, x = function (i) { return n === 1 ? (L + W - R) / 2 : L + i * (W - L - R) / (n - 1); };
  var y = function (v) { return T + (hi - v) / (hi - lo) * (H - T - B); };
  var out = ['<svg class="chart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Official plus-minus and Rel by season">'];
  for (var g = lo; g <= hi; g += step) {
    out.push('<line class="' + (g === 0 ? "zero" : "grid") + '" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(g) + '" y2="' + y(g) + '"/>');
    out.push('<text x="' + (L - 6) + '" y="' + (y(g) + 4) + '" text-anchor="end">' + (g > 0 ? "+" : "") + g + "</text>");
  }
  var path = function (key) {
    var d = [], pen = "M";
    rows.forEach(function (r, i) { if (r[key] === null) { pen = "M"; return; } d.push(pen + x(i).toFixed(1) + "," + y(r[key]).toFixed(1)); pen = "L"; });
    return d.join(" ");
  };
  out.push('<path class="pm" d="' + path("pm") + '"/>');
  out.push('<path class="rel" d="' + path("rel") + '"/>');
  var every = Math.max(1, Math.ceil(n / Math.max(4, Math.floor((W - L - R) / 64))));
  rows.forEach(function (r, i) {
    var cx = x(i).toFixed(1);
    out.push('<rect class="pm" x="' + (x(i) - 3.5).toFixed(1) + '" y="' + (y(r.pm) - 3.5).toFixed(1) + '" width="7" height="7"><title>' +
      esc(label(r.season) + " " + r.team + ": Official_PM " + signed(r.pm, 0)) + "</title></rect>");
    if (r.rel !== null) out.push('<circle class="rel" cx="' + cx + '" cy="' + y(r.rel).toFixed(1) + '" r="4"><title>' +
      esc(label(r.season) + " " + r.team + ": Rel " + signed(r.rel, 2)) + "</title></circle>");
    if (i % every === 0 || i === n - 1) {
      out.push('<text x="' + cx + '" y="' + (H - B + 16) + '" text-anchor="middle">' + esc(label(r.season).replace(/^20/, "")) + "</text>");
      out.push('<text class="tm" x="' + cx + '" y="' + (H - B + 30) + '" text-anchor="middle">' + esc(r.team) + "</text>");
    }
  });
  out.push("</svg>");
  $("chart").innerHTML = out.join("");
}
})();
</script>
</body>
</html>
"""


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<meta name="description" content="5-on-5 adjusted plus-minus for every NHL skater, rebuilt daily from NHL play-by-play and shift data.">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cellipse cx='16' cy='18' rx='13' ry='6' fill='%23111'/%3E%3Cellipse cx='16' cy='14' rx='13' ry='6' fill='%23333'/%3E%3C/svg%3E">
<style>
:root {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --ink-3: #78776f; --rule: #e4e3de; --rule-2: #d3d2cb; --track: #ecebe6;
  --pos: __POS__; --neg: __NEG__; --pos-wash: #e4f3e3; --neg-wash: #f8e9e8;
  --focus: #2a78d6; --control: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
    --ink-3: #96958c; --rule: #2c2c2a; --rule-2: #3a3a37; --track: #2c2c2a;
    --pos: #0ca30c; --neg: #e05555; --pos-wash: #183017; --neg-wash: #362221;
    --focus: #3987e5; --control: #222220;
  }
}
:root[data-theme="dark"] {
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7;
  --ink-3: #96958c; --rule: #2c2c2a; --rule-2: #3a3a37; --track: #2c2c2a;
  --pos: #0ca30c; --neg: #e05555; --pos-wash: #183017; --neg-wash: #362221;
  --focus: #3987e5; --control: #222220;
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 400 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
  font-variant-numeric: tabular-nums; padding: 0 16px 56px; }
.wrap { max-width: 1120px; margin: 0 auto; }
header { padding: 28px 0 8px; }
h1 { font-size: 1.4rem; line-height: 1.25; margin: 0 0 4px; letter-spacing: -.01em; }
.sub { color: var(--ink-2); font-size: .85rem; margin: 0; }
.stamp { color: var(--ink-3); font-size: .78rem; margin: 6px 0 0; }

details.filters { margin: 14px 0 12px; background: var(--surface);
  border: 1px solid var(--rule); border-radius: 10px; }
details.filters > summary { cursor: pointer; padding: 10px 14px; font-weight: 600;
  font-size: .88rem; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
details.filters > summary .active { color: var(--ink-3); font-weight: 400; font-size: .8rem; }
details.filters > summary:focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; }
.fgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px;
  padding: 4px 14px 14px; }
@media (min-width: 760px) { .fgrid { grid-template-columns: repeat(auto-fill, minmax(165px, 1fr)); } }
.f { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.f.wide { grid-column: 1 / -1; }
@media (min-width: 760px) { .f.wide { grid-column: span 2; } }
.f > span, .f > label > span.lab { font-size: .66rem; text-transform: uppercase;
  letter-spacing: .05em; color: var(--ink-3); font-weight: 600; }
.f select, .f input[type=search], .f input[type=number], .f input[type=date] {
  font: inherit; font-size: .86rem; color: var(--ink); background: var(--control);
  border: 1px solid var(--rule-2); border-radius: 8px; padding: 6px 8px; width: 100%;
  min-width: 0; min-height: 34px; }
.f select:focus-visible, .f input:focus-visible, .seg button:focus-visible,
.linkbtn:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.dates { display: flex; gap: 6px; }
.dates[hidden] { display: none; }
.seg { display: flex; border: 1px solid var(--rule-2); border-radius: 8px; overflow: hidden;
  min-height: 34px; }
.seg button { all: unset; flex: 1; text-align: center; font-size: .82rem; padding: 6px 4px;
  cursor: pointer; color: var(--ink-2); background: var(--control); }
.seg button + button { border-left: 1px solid var(--rule-2); }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--page); font-weight: 600; }
.check { display: flex; align-items: center; gap: 8px; font-size: .86rem; min-height: 34px; }
.check input { width: 18px; height: 18px; margin: 0; accent-color: var(--ink); }
.linkbtn { all: unset; cursor: pointer; color: var(--ink-2); text-decoration: underline;
  font-size: .8rem; }

.tiles { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 0 0 12px; }
@media (min-width: 620px) { .tiles { grid-template-columns: repeat(4, 1fr); } }
.tile { background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  padding: 10px 12px; display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.tlabel { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: var(--ink-3); }
.tval { font-size: 1.45rem; font-weight: 650; line-height: 1.1; }
.tsub { font-size: .72rem; color: var(--ink-3); }
.count { color: var(--ink-3); font-size: .78rem; margin: 0 2px 8px; }

.tablewrap { overflow-x: auto; background: var(--surface); border: 1px solid var(--rule);
  border-radius: 10px; -webkit-overflow-scrolling: touch; }
table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: .82rem; }
th, td { padding: 7px 9px; white-space: nowrap; border-bottom: 1px solid var(--rule); }
tbody tr:last-child th, tbody tr:last-child td { border-bottom: 0; }
thead th { position: sticky; top: 0; z-index: 3; background: var(--surface);
  border-bottom: 1px solid var(--rule-2); padding: 0; }
/* width:auto + min-width:100% so the header text sets the column minimum;
   with width:100% the next sticky th paints over it and headers look cut. */
thead th button { all: unset; box-sizing: border-box; display: block; width: auto;
  min-width: 100%; cursor: pointer; padding: 9px; font: inherit; font-size: .68rem;
  font-weight: 650; color: var(--ink-2); text-transform: uppercase; letter-spacing: .04em; }
thead th.num button { text-align: right; }
thead th button:focus-visible { outline: 2px solid var(--focus); outline-offset: -2px; }
thead th[aria-sort="descending"] .arrow::after { content: " \2193"; }
thead th[aria-sort="ascending"] .arrow::after { content: " \2191"; }
thead th:not([aria-sort="none"]) button { color: var(--ink); }
.num { text-align: right; }
.txt { text-align: left; }
th.player, thead th:first-child { position: sticky; left: 0; z-index: 2;
  background: var(--surface); border-right: 1px solid var(--rule); }
th.player { font-weight: 500; }
th.player a { color: inherit; text-decoration: none; }
.dag { color: var(--ink-3); margin-left: 2px; font-size: .8em; }
th.player a:hover { text-decoration: underline; }
th.player a:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.gnote { color: var(--ink-3); font-size: .74rem; margin: 0; grid-column: 1 / -1; }
thead th:first-child { z-index: 4; }
tbody tr:nth-child(even) th, tbody tr:nth-child(even) td { background: var(--page); }
td.tm { color: var(--ink-2); }
.abbr { display: none; }
td.hl { min-width: 92px; }
.adjnum { display: block; font-weight: 650; }
.bar { position: relative; display: block; height: 4px; margin: 3px 0 0 auto; width: 100%;
  max-width: 76px; background: var(--track); border-radius: 2px; }
.bar::before { content: ""; position: absolute; left: 50%; top: -2px; width: 1px; height: 8px;
  background: var(--rule-2); }
.bar i { position: absolute; top: 0; height: 4px; border-radius: 2px; min-width: 2px; }
.bar i.pos { left: 50%; background: var(--pos); }
.bar i.neg { right: 50%; background: var(--neg); }
tbody tr td.hl.p { background: var(--pos-wash); }
tbody tr td.hl.n { background: var(--neg-wash); }
.state { padding: 22px 14px; color: var(--ink-3); font-size: .86rem; }
.more { display: block; margin: 10px auto 0; font: inherit; font-size: .84rem; cursor: pointer;
  color: var(--ink); background: var(--surface); border: 1px solid var(--rule-2);
  border-radius: 8px; padding: 8px 16px; }
.more[hidden] { display: none; }
.hint { display: none; color: var(--ink-3); font-size: .72rem; margin: 8px 2px 0; }
@media (max-width: 760px) {
  .full { display: none; }
  .abbr { display: inline; }
  .hint { display: block; }
  table { font-size: .76rem; }
  th, td { padding: 6px 7px; }
  thead th button { padding: 8px 7px; font-size: .64rem; }
  th.player { max-width: 118px; overflow: hidden; text-overflow: ellipsis; }
  td.hl { min-width: 72px; }
  .bar { max-width: 54px; }
  .tval { font-size: 1.3rem; }
}
.prov { color: var(--ink-3); font-size: .76rem; margin: 10px 2px 0; }
details.excl { color: var(--ink-3); font-size: .76rem; margin: 6px 2px 0; }
details.excl summary { cursor: pointer; }
details.excl ul { margin: 6px 0 0; padding-left: 18px; }
details.legend { margin: 26px 0 0; background: var(--surface); border: 1px solid var(--rule);
  border-radius: 10px; padding: 0 14px; }
details.legend summary { cursor: pointer; padding: 12px 0; font-weight: 600; font-size: .9rem; }
details.legend summary:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
dl { margin: 0 0 12px; }
.lrow { padding: 9px 0; border-top: 1px solid var(--rule); }
dt { font-weight: 650; font-size: .82rem; margin: 0 0 2px; }
dd { margin: 0; color: var(--ink-2); font-size: .84rem; }
footer { margin-top: 26px; color: var(--ink-3); font-size: .74rem; }
footer a, .prov a { color: var(--ink-2); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>__TITLE__</h1>
  <p class="sub">5-on-5 only, both goalies in net &middot; <span id="seasonLabel">__SEASON_LABEL__</span> regular season</p>
  <p class="stamp">Updated __STAMP__<span id="through"></span></p>
</header>

<details class="filters" id="filters" open>
  <summary>Filters <span class="active" id="activeSummary"></span></summary>
  <div class="fgrid">
    <div class="f wide" id="seasonWrap"><span>Seasons</span>
      <div class="dates">
        <select id="season" aria-label="Season"></select>
        <select id="sfrom" aria-label="From season" hidden></select>
        <select id="sto" aria-label="To season" hidden></select>
      </div></div>
    <label class="f"><span class="lab">Scope</span><select id="scope"></select></label>
    <label class="f"><span class="lab">Position</span>
      <select id="pos">
        <option value="">All skaters</option><option value="F">Forwards</option>
        <option value="D">Defense</option><option value="C">C</option>
        <option value="LW">LW</option><option value="RW">RW</option>
      </select></label>
    <div class="f wide gonly"><span>Games</span>
      <div class="dates">
        <select id="games" aria-label="Games">
          <option value="">Season to date</option><option value="5">Last 5 games</option>
          <option value="10">Last 10 games</option><option value="20">Last 20 games</option>
          <option value="custom">Custom dates</option>
        </select>
      </div>
      <div class="dates" id="dateWrap" hidden>
        <input type="date" id="from" aria-label="From date">
        <input type="date" id="to" aria-label="To date">
      </div>
    </div>
    <label class="f"><span class="lab">Min 5v5 TOI (min)</span>
      <input type="number" id="min" min="0" step="10" inputmode="numeric"></label>
    <div class="f gonly"><span>Venue</span>
      <div class="seg" role="group" aria-label="Venue" id="venue">
        <button type="button" data-v="">Both</button><button type="button" data-v="home">Home</button><button type="button" data-v="away">Away</button>
      </div></div>
    <div class="f"><span>Show</span>
      <div class="seg" role="group" aria-label="Totals or per 60" id="mode">
        <button type="button" data-v="">Totals</button><button type="button" data-v="60">Per 60</button>
      </div></div>
    <div class="f gonly"><span>Rookies</span>
      <label class="check"><input type="checkbox" id="rookies"> Rookies only</label></div>
    <label class="f wide"><span class="lab">Player</span>
      <input type="search" id="q" placeholder="Search by name" autocomplete="off" spellcheck="false"></label>
    <div class="f"><span>&nbsp;</span><button type="button" class="linkbtn" id="reset">Reset filters</button></div>
    <p class="gnote" id="gnote" hidden>Games, venue and rookie filters apply within a single season.</p>
  </div>
</details>

<div class="tiles" id="tiles"></div>
<p class="count" id="count"></p>
<div class="tablewrap">
<table id="table">
<caption class="sr-only">Skaters, 5-on-5 adjusted plus-minus, sortable</caption>
<thead><tr id="head"></tr></thead>
<tbody id="body"><tr><td class="state" colspan="12">Loading the season&hellip;</td></tr></tbody>
</table>
</div>
<button type="button" class="more" id="more" hidden></button>
<p class="hint">Player stays pinned, with Rel beside it. Swipe the table sideways for the other columns.</p>
<p class="prov" id="prov"></p>
<div id="excluded"></div>

<details class="legend">
  <summary>What the columns and filters mean</summary>
  <dl>__LEGEND__</dl>
</details>

<footer>
  <p>Built from the NHL's public play-by-play, shift-chart, boxscore, standings and
  skater-bio feeds. Only goals with <code>situationCode 1551</code> (five skaters a
  side, both goalies in net) count toward GD_on. Tap a column heading to sort.
  Filters are saved in the address bar, so you can share a view by sharing the link.</p>
  <p>Small samples move these numbers a lot: a single goal can swing Adjusted by
  more than a point.</p>
  <p><button type="button" class="linkbtn" id="csv">Download this view as CSV</button>
  &middot; <a href="data/index.json">Raw data</a></p>
</footer>
</div>
<script>
(function () {
"use strict";
var DEFAULT_MIN = __DEFAULT_MIN__, DEFAULT_MIN_TEAM = __DEFAULT_MIN_TEAM__,
    DEFAULT_MIN_CAREER = __DEFAULT_MIN_CAREER__;
var PAGE_ROWS = 100;
var COLS = [
  {k: "name", h: "Player", a: "Player", t: "txt"},
  {k: "rel", h: "Rel", a: "Rel", t: "num", d: 2, s: 1, rate: 1, hl: 1, tip: "5v5 goal differential vs. the team with the skater on the bench, scaled to his minutes"},
  {k: "team", h: "Team", a: "Team", t: "txt"},
  {k: "pos", h: "Pos", a: "Pos", t: "txt"},
  {k: "gp", h: "GP", a: "GP", t: "num", d: 0, tip: "Games in this selection"},
  {k: "toi", h: "TOI_5v5", a: "TOI", t: "num", d: 1, tip: "Minutes at 5v5 with both goalies in net"},
  {k: "pm", h: "Official_PM", a: "Off +/-", t: "num", d: 0, s: 1, tip: "The NHL's published plus-minus"},
  {k: "gd", h: "GD_on", a: "GD on", t: "num", d: 0, s: 1, rate: 1, tip: "5v5 goal differential on ice"},
  {k: "nc", h: "NonClean", a: "NonCln", t: "num", d: 0, s: 1, tip: "Official minus 5v5"},
  {k: "exp", h: "Expected", a: "Exp", t: "num", d: 2, s: 1, rate: 1, tip: "Team GD in each game x share of that game's 5v5 minutes"},
  {k: "adj", h: "Adjusted", a: "Adj", t: "num", d: 2, s: 1, rate: 1, tip: "GD_on minus Expected"},
  {k: "gd60", h: "GD_on/60", a: "GD/60", t: "num", d: 2, s: 1, tip: "GD_on per 60 minutes of 5v5"}
];
var HEAD = COLS.filter(function (c) { return c.hl; })[0];
var POS_LABEL = {C: "C", L: "LW", R: "RW", D: "D"};
var $ = function (id) { return document.getElementById(id); };

var MANIFEST = null, D = null, C = null, PLAYERS = null;
var S = defaults();
var result = [], showAll = false;

function defaults() {
  // min: null = follow the view's default (minDefault); a number = the reader's.
  // season: a season id, "range" (sfrom..sto) or "career".
  return {season: "", sfrom: "", sto: "", scope: "", pos: "", games: "", from: "", to: "", min: null,
          venue: "", rookies: false, q: "", per60: false, sort: HEAD.k, dir: "desc"};
}
function multi() { return S.season === "range" || S.season === "career"; }
function rangeIds() {
  if (!C) return [];
  if (S.season === "career") return C.seasons.slice();
  var lo = S.sfrom || C.seasons[0], hi = S.sto || C.seasons[C.seasons.length - 1];
  if (lo > hi) { var t = lo; lo = hi; hi = t; }
  return C.seasons.filter(function (id) { return id >= lo && id <= hi; });
}
function minDefault() {
  if (S.scope && S.scope[0] === "t") return DEFAULT_MIN_TEAM;
  if (S.season === "career") return DEFAULT_MIN_CAREER;
  if (S.season === "range") return Math.min(DEFAULT_MIN_CAREER, DEFAULT_MIN * Math.max(1, rangeIds().length));
  return DEFAULT_MIN;
}
function minToi() { return S.min === null ? minDefault() : S.min; }

function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) {
    return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c];
  });
}
function fold(s) {
  return String(s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

// -- URL state ------------------------------------------------------------
function readUrl() {
  var p = new URLSearchParams(location.search), s = defaults();
  s.season = p.get("season") || "";
  if (p.get("career") === "1") s.season = "career";
  else if (p.get("sfrom") || p.get("sto")) { s.season = "range"; s.sfrom = p.get("sfrom") || ""; s.sto = p.get("sto") || ""; }
  if (p.get("team")) s.scope = "t:" + p.get("team").toUpperCase();
  else if (p.get("div")) s.scope = "d:" + p.get("div");
  else if (p.get("conf")) s.scope = "c:" + p.get("conf");
  var pos = (p.get("pos") || "").toUpperCase();
  if (["F", "D", "C", "LW", "RW"].indexOf(pos) >= 0) s.pos = pos;
  var g = p.get("games") || "";
  if (["5", "10", "20"].indexOf(g) >= 0) s.games = g;
  if (p.get("from") || p.get("to")) { s.games = "custom"; s.from = p.get("from") || ""; s.to = p.get("to") || ""; }
  if (p.get("min") !== null && !isNaN(parseFloat(p.get("min")))) s.min = Math.max(0, parseFloat(p.get("min")));
  var v = p.get("venue"); if (v === "home" || v === "away") s.venue = v;
  s.rookies = p.get("rookies") === "1";
  s.q = p.get("q") || "";
  s.per60 = p.get("per60") === "1";
  var sort = p.get("sort");
  if (sort && COLS.some(function (c) { return c.k === sort; })) s.sort = sort;
  if (p.get("dir") === "asc" || p.get("dir") === "desc") s.dir = p.get("dir");
  return s;
}
function writeUrl() {
  var p = new URLSearchParams();
  if (S.season === "career") p.set("career", "1");
  else if (S.season === "range") { if (S.sfrom) p.set("sfrom", S.sfrom); if (S.sto) p.set("sto", S.sto); }
  else if (S.season && MANIFEST && S.season !== MANIFEST["default"]) p.set("season", S.season);
  if (S.scope) p.set({t: "team", d: "div", c: "conf"}[S.scope[0]], S.scope.slice(2));
  if (S.pos) p.set("pos", S.pos);
  if (!multi()) {
    if (S.games === "custom") { if (S.from) p.set("from", S.from); if (S.to) p.set("to", S.to); }
    else if (S.games) p.set("games", S.games);
  }
  if (S.min !== null) p.set("min", String(S.min));
  if (S.venue && !multi()) p.set("venue", S.venue);
  if (S.rookies && !multi()) p.set("rookies", "1");
  if (S.q) p.set("q", S.q);
  if (S.per60) p.set("per60", "1");
  if (S.sort !== HEAD.k) p.set("sort", S.sort);
  if (S.dir !== "desc" || S.sort !== HEAD.k) p.set("dir", S.dir);
  var qs = p.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}

// -- data -----------------------------------------------------------------
function getJson(url, fresh) {
  return fetch(url, fresh ? {cache: "no-cache"} : {}).then(function (r) {
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  });
}
function bust() { return "?v=" + encodeURIComponent((MANIFEST.seasons[0] || {}).updated || ""); }
function loadPlayers() {
  return PLAYERS ? Promise.resolve(PLAYERS)
    : getJson("data/" + MANIFEST.players + bust()).then(function (pl) { PLAYERS = pl; return pl; });
}
function load(seasonId) {
  if (seasonId === "range" || seasonId === "career") return loadCareers();
  var entry = MANIFEST.seasons.filter(function (s) { return s.id === seasonId; })[0]
           || MANIFEST.seasons[0];
  S.season = entry.id;
  return Promise.all([
    getJson("data/" + entry.file + "?v=" + encodeURIComponent(entry.updated || "")),
    loadPlayers()
  ]).then(function (res) { D = prepare(res[0], res[1]); });
}
function loadCareers() {
  if (C) return Promise.resolve();
  return Promise.all([getJson("data/" + (MANIFEST.careers || "player_seasons.json") + bust()), loadPlayers()])
    .then(function (res) { C = prepareCareers(res[0], res[1]); });
}
// player_seasons.json: one row per player-season-club, every column a total.
function prepareCareers(ps, pl) {
  var c = {seasons: ps.seasons, meta: ps.meta, teamIdx: {}, teamList: []};
  var rows = ps.rows, n = rows.length;
  c.s = new Int16Array(n); c.tm = new Int16Array(n); c.gp = new Int16Array(n);
  c.toi = new Int32Array(n); c.gf = new Int16Array(n); c.ga = new Int16Array(n);
  c.pm = new Int16Array(n); c.xm = new Int32Array(n); c.toff = new Int32Array(n); c.goff = new Int16Array(n);
  var byPlayer = new Map();
  for (var i = 0; i < n; i++) {
    var r = rows[i];   // s, pid, tm, gp, toi, gf, ga, pm, xm, toff, goff, rk
    var ti = c.teamIdx[r[2]];
    if (ti === undefined) { ti = c.teamIdx[r[2]] = c.teamList.length; c.teamList.push(r[2]); }
    c.s[i] = r[0]; c.tm[i] = ti; c.gp[i] = r[3]; c.toi[i] = r[4]; c.gf[i] = r[5]; c.ga[i] = r[6];
    c.pm[i] = r[7]; c.xm[i] = r[8]; c.toff[i] = r[9]; c.goff[i] = r[10];
    var list = byPlayer.get(r[1]);
    if (!list) { list = []; byPlayer.set(r[1], list); }
    list.push(i);
  }
  c.players = [];
  var meta = pl.players;
  byPlayer.forEach(function (list, pid) {
    var m = meta[String(pid)] || ["Player " + pid, "?", "", ""];
    c.players.push({pid: pid, name: m[0], key: fold(m[0]), pos: m[1], rows: Int32Array.from(list)});
  });
  // Scope options by name, because conference/division codes change across eras.
  c.confNames = {}; c.divNames = {}; c.teamNames = {};
  c.seasons.forEach(function (sid) {
    var m = c.meta[sid] || {};
    Object.keys(m.confs || {}).forEach(function (k) { c.confNames[m.confs[k]] = 1; });
    Object.keys(m.divs || {}).forEach(function (k) { c.divNames[m.divs[k]] = 1; });
    Object.keys(m.teams || {}).forEach(function (k) { c.teamNames[k] = m.teams[k][0]; });
  });
  return c;
}
function prepare(sf, pl) {
  var d = {playersRaw: pl, season: sf.season, updated: sf.updated};
  d.teams = sf.teams.rows.map(function (r) { return {abbr: r[0], name: r[1], conf: r[2], div: r[3]}; });
  d.confs = sf.confs; d.divs = sf.divs;
  d.games = sf.games.rows.map(function (r) {
    return {id: r[0], date: r[1], away: r[2], home: r[3], sec: r[4], agf: r[5], hgf: r[6], src: r[7]};
  });
  d.sources = sf.sources || [];
  var rows = sf.rows.rows, n = rows.length;
  d.rG = new Int32Array(n); d.rH = new Uint8Array(n); d.rToi = new Int32Array(n);
  d.rGf = new Int16Array(n); d.rGa = new Int16Array(n); d.rPm = new Int16Array(n);
  var byPlayer = new Map();
  for (var i = 0; i < n; i++) {
    var r = rows[i];
    d.rG[i] = r[1]; d.rH[i] = r[2]; d.rToi[i] = r[3]; d.rGf[i] = r[4]; d.rGa[i] = r[5]; d.rPm[i] = r[6];
    var list = byPlayer.get(r[0]);
    if (!list) { list = []; byPlayer.set(r[0], list); }
    list.push(i);   // rows are stored in game (= date) order
  }
  d.players = [];
  var meta = pl.players, rookies = new Set(sf.rookies || []);
  byPlayer.forEach(function (list, pid) {
    var m = meta[String(pid)] || ["Player " + pid, "?", "", ""];
    d.players.push({pid: pid, name: m[0], key: fold(m[0]), pos: m[1], rookie: rookies.has(pid),
                    rows: Int32Array.from(list)});
  });
  d.excluded = (sf.excluded && sf.excluded.rows) || [];
  return d;
}

// -- aggregation (the twin of nhlapm/aggregate.py) ---------------------------
function gameWindow() {
  var lo = 0, hi = D.games.length - 1, i;
  if (S.games === "custom") {
    if (S.from) { lo = D.games.length; for (i = 0; i < D.games.length; i++) if (D.games[i].date >= S.from) { lo = i; break; } }
    if (S.to) { hi = -1; for (i = D.games.length - 1; i >= 0; i--) if (D.games[i].date <= S.to) { hi = i; break; } }
  }
  return [lo, hi];
}
function teamFilter() {
  if (!S.scope) return null;
  var kind = S.scope[0], val = S.scope.slice(2);
  return D.teams.map(function (t) {
    return kind === "t" ? t.abbr === val : kind === "d" ? t.div === val : t.conf === val;
  });
}
function posOk(pos) {
  if (!S.pos) return true;
  if (S.pos === "F") return pos === "C" || pos === "L" || pos === "R";
  if (S.pos === "D") return pos === "D";
  return POS_LABEL[pos] === S.pos;
}
function compute() {
  var win = gameWindow(), lo = win[0], hi = win[1];
  var lastN = (S.games && S.games !== "custom") ? parseInt(S.games, 10) : 0;
  var teamOk = teamFilter(), venue = S.venue, q = fold(S.q.trim());
  var minSec = minToi() * 60, G = D.games;
  var out = [], i, j;
  for (i = 0; i < D.players.length; i++) {
    var p = D.players[i];
    if (!posOk(p.pos) || (S.rookies && !p.rookie) || (q && p.key.indexOf(q) < 0)) continue;
    var toi = 0, gf = 0, ga = 0, pm = 0, exp = 0, gp = 0, first = {}, club = {};
    // Newest first, so "last N" is simply the first N that pass.
    for (j = p.rows.length - 1; j >= 0; j--) {
      var r = p.rows[j], g = D.rG[r];
      if (g < lo || g > hi) continue;
      var h = D.rH[r];
      if ((venue === "home" && !h) || (venue === "away" && h)) continue;
      var game = G[g], tm = h ? game.home : game.away;
      if (teamOk && !teamOk[tm]) continue;
      var tgd = h ? game.hgf - game.agf : game.agf - game.hgf;
      var t = D.rToi[r], gd = D.rGf[r] - D.rGa[r];
      toi += t; gf += D.rGf[r]; ga += D.rGa[r]; pm += D.rPm[r];
      if (game.sec) exp += tgd * t / game.sec;
      var acc = club[tm] || (club[tm] = [0, 0, 0, 0]);   // GD_on, GD_off, TOI, TOI_off
      acc[0] += gd; acc[1] += tgd - gd; acc[2] += t; acc[3] += game.sec - t;
      first[tm] = g;
      gp++;
      if (lastN && gp >= lastN) break;
    }
    if (!gp || toi < minSec) continue;
    var gdOn = gf - ga, adj = gdOn - exp;
    var rel = clubRel(club);
    var label = Object.keys(first).sort(function (a, b) { return first[a] - first[b]; })
      .map(function (k) { return D.teams[k].abbr; }).join("/");
    out.push({pid: p.pid, name: p.name, team: label, pos: POS_LABEL[p.pos] || p.pos, gp: gp,
              toi: toi / 60, pm: pm, gd: gdOn, nc: pm - gdOn, exp: exp, adj: adj, rel: rel,
              gd60: toi ? gdOn * 3600 / toi : null, sec: toi});
  }
  return out;
}
// Rel per club (or per club-season), added up: never a ratio over pooled bench
// minutes from different clubs. The twin of aggregate.stint_rel.
function clubRel(club) {
  var sum = 0, any = false;
  for (var k in club) {
    var a = club[k];
    if (a[3]) { sum += a[0] - a[1] * (a[2] / a[3]); any = true; }
  }
  return any ? sum : null;
}
function stintScopeOk(sid, abbr) {
  if (!S.scope) return true;
  var kind = S.scope[0], val = S.scope.slice(2);
  if (kind === "t") return abbr === val;
  var m = C.meta[sid] || {}, t = (m.teams || {})[abbr];
  if (!t) return false;
  return kind === "c" ? (m.confs || {})[t[1]] === val : (m.divs || {})[t[2]] === val;
}
function computeStints() {
  var ids = rangeIds(), inRange = {}, i, j;
  ids.forEach(function (id) { inRange[C.seasons.indexOf(id)] = 1; });
  var q = fold(S.q.trim()), minSec = minToi() * 60, out = [];
  for (i = 0; i < C.players.length; i++) {
    var p = C.players[i];
    if (!posOk(p.pos) || (q && p.key.indexOf(q) < 0)) continue;
    var toi = 0, gf = 0, ga = 0, pm = 0, xm = 0, gp = 0, first = {}, club = {}, nseason = {};
    for (j = 0; j < p.rows.length; j++) {
      var r = p.rows[j], si = C.s[r];
      if (!inRange[si]) continue;
      var abbr = C.teamList[C.tm[r]], sid = C.seasons[si];
      if (!stintScopeOk(sid, abbr)) continue;
      toi += C.toi[r]; gf += C.gf[r]; ga += C.ga[r]; pm += C.pm[r]; xm += C.xm[r]; gp += C.gp[r];
      club[si + ":" + abbr] = [C.gf[r] - C.ga[r], C.goff[r], C.toi[r], C.toff[r]];
      if (!(abbr in first) || si < first[abbr]) first[abbr] = si;
      nseason[si] = 1;
    }
    if (!gp || toi < minSec) continue;
    var gdOn = gf - ga, exp = xm / 10000;
    var label = Object.keys(first).sort(function (a, b) { return first[a] - first[b] || (a < b ? -1 : 1); }).join("/");
    out.push({pid: p.pid, name: p.name, team: label, pos: POS_LABEL[p.pos] || p.pos, gp: gp,
              toi: toi / 60, pm: pm, gd: gdOn, nc: pm - gdOn, exp: exp, adj: gdOn - exp, rel: clubRel(club),
              gd60: toi ? gdOn * 3600 / toi : null, sec: toi, ns: Object.keys(nseason).length,
              part: Object.keys(nseason).some(function (si) { return PARTIAL[C.seasons[si]]; })});
  }
  return out;
}
function value(row, col) {
  var v = row[col.k];
  if (S.per60 && col.rate) return (v === null || !row.sec) ? null : v * 3600 / row.sec;
  return v;
}
function sortRows(rows) {
  var col = COLS.filter(function (c) { return c.k === S.sort; })[0] || HEAD;
  var desc = S.dir === "desc";
  rows.sort(function (a, b) {
    var x = value(a, col), y = value(b, col);
    if (x === null || y === null) return x === y ? 0 : (x === null ? 1 : -1);
    var c = col.t === "num" ? x - y : String(x).localeCompare(String(y));
    if (c === 0) return b.toi - a.toi;
    return desc ? -c : c;
  });
}

// -- rendering --------------------------------------------------------------
function visibleCols() { return COLS.filter(function (c) { return !(S.per60 && c.k === "gd60"); }); }
function fmt(v, col, rate) {
  if (v === null || v === undefined || (typeof v === "number" && !isFinite(v))) return "--";
  if (col.t !== "num") return esc(v);
  var d = rate ? 2 : col.d, r = Number(v.toFixed(d));
  if (r === 0) return (0).toFixed(d);     // no "-0.00" washed red for nothing
  var s = r.toFixed(d);
  return col.s && r > 0 ? "+" + s : s;
}
function renderHead() {
  var cells = visibleCols().map(function (c) {
    var rate = S.per60 && c.rate;
    var sort = c.k === S.sort ? (S.dir === "desc" ? "descending" : "ascending") : "none";
    var cls = c.t === "num" ? "num" : "txt";
    return '<th scope="col" class="' + cls + '" aria-sort="' + sort + '" data-k="' + c.k + '"' +
      (c.tip ? ' title="' + esc(c.tip) + '"' : "") + '><button type="button">' +
      '<span class="full">' + esc(c.h + (rate ? "/60" : "")) + '</span><span class="abbr">' +
      esc(c.a + (rate ? "/60" : "")) + '</span><span class="arrow" aria-hidden="true"></span></button></th>';
  });
  $("head").innerHTML = cells.join("");
}
function renderBody() {
  var cols = visibleCols();
  if (!result.length) {
    $("body").innerHTML = '<tr><td class="state" colspan="' + cols.length +
      '">No skaters match these filters. Try lowering the minimum TOI.</td></tr>';
    $("more").hidden = true;
    return;
  }
  var peak = 0;
  result.forEach(function (r) { var v = value(r, HEAD); if (v !== null) peak = Math.max(peak, Math.abs(v)); });
  peak = peak || 1;
  var n = showAll ? result.length : Math.min(PAGE_ROWS, result.length), html = [];
  for (var i = 0; i < n; i++) {
    var row = result[i], cells = [];
    for (var c = 0; c < cols.length; c++) {
      var col = cols[c], rate = S.per60 && col.rate, v = value(row, col), text = fmt(v, col, rate);
      if (col.k === "name") cells.push('<th scope="row" class="txt player"><a href="player.html?id=' +
        row.pid + '">' + text + "</a>" + (row.part ? '<span class="dag" title="Includes a season with games left out (see the note below the table)">†</span>' : "") + "</th>");
      else if (col.hl) {
        var shown = v === null ? 0 : Number(v.toFixed(2));
        var wash = shown === 0 ? "z" : (shown > 0 ? "p" : "n");
        var w = Math.min(100, Math.abs(v || 0) / peak * 100) / 2;
        var bar = shown === 0 ? "" : '<i class="' + (shown > 0 ? "pos" : "neg") + '" style="width:' + w.toFixed(1) + '%"></i>';
        cells.push('<td class="num hl ' + wash + '"><span class="adjnum">' + text +
                   '</span><span class="bar" aria-hidden="true">' + bar + "</span></td>");
      } else cells.push('<td class="' + (col.t === "num" ? "num" : "txt") + (col.k === "team" ? " tm" : "") + '">' + text + "</td>");
    }
    html.push("<tr>" + cells.join("") + "</tr>");
  }
  $("body").innerHTML = html.join("");
  var more = $("more");
  more.hidden = showAll || result.length <= PAGE_ROWS;
  more.textContent = "Show all " + result.length + " skaters";
}
function tile(label, val, sub) {
  return '<div class="tile"><span class="tlabel">' + esc(label) + '</span><span class="tval">' +
    esc(val) + "</span>" + (sub ? '<span class="tsub">' + esc(sub) + "</span>" : "") + "</div>";
}
function signed(n, d) { var r = Number(n.toFixed(d || 0)); return (r > 0 ? "+" : "") + r.toFixed(d || 0); }
function renderTiles() {
  if (multi()) {
    var ids = rangeIds(), clubs = {};
    result.forEach(function (r) { r.team.split("/").forEach(function (t) { clubs[t] = 1; }); });
    var span = ids.length ? label(ids[0]) + (ids.length > 1 ? " to " + label(ids[ids.length - 1]) : "") : "";
    $("tiles").innerHTML = tile("Seasons", String(ids.length), span) +
      tile("Skaters listed", result.length.toLocaleString(), "of " + C.players.length.toLocaleString() + " in the data") +
      tile("Clubs", String(Object.keys(clubs).length), "among the skaters listed") +
      tile("Min 5v5 TOI", String(minToi()), S.min === null ? "scales with the seasons picked" : "set by you");
    return;
  }
  var win = gameWindow(), lo = win[0], hi = win[1], G = D.games, i;
  var teamOk = teamFilter(), lastN = (S.games && S.games !== "custom") ? parseInt(S.games, 10) : 0;
  if (S.scope && S.scope[0] === "t") {
    var ti = D.teams.findIndex(function (t) { return t.abbr === S.scope.slice(2); });
    var gf = 0, ga = 0, sec = 0, n = 0;
    for (i = hi; i >= lo; i--) {
      var g = G[i], home = g.home === ti;
      if (g.home !== ti && g.away !== ti) continue;
      if ((S.venue === "home" && !home) || (S.venue === "away" && home)) continue;
      gf += home ? g.hgf : g.agf; ga += home ? g.agf : g.hgf; sec += g.sec; n++;
      if (lastN && n >= lastN) break;
    }
    $("tiles").innerHTML = tile("5v5 goals for", String(gf)) + tile("5v5 goals against", String(ga)) +
      tile("5v5 differential", signed(gf - ga), sec ? signed((gf - ga) * 3600 / sec, 2) + " per 60" : "") +
      tile("5v5 minutes", Math.round(sec / 60).toLocaleString(), n + " game" + (n === 1 ? "" : "s"));
  } else {
    var games = 0, goals = 0, mins = 0;
    for (i = lo; i <= hi; i++) {
      var gm = G[i];
      if (teamOk && !teamOk[gm.home] && !teamOk[gm.away]) continue;
      games++; goals += gm.hgf + gm.agf; mins += gm.sec;
    }
    $("tiles").innerHTML = tile("Games", games.toLocaleString(), lastN ? "season window; last " + lastN + " applies per skater" : "") +
      tile("Skaters listed", result.length.toLocaleString(), "of " + D.players.length.toLocaleString() + " who played") +
      tile("5v5 goals", goals.toLocaleString(), mins ? (goals * 3600 / mins).toFixed(2) + " per 60" : "") +
      tile("Games excluded", String(D.excluded.length), D.excluded.length ? "failed verification" : "none");
  }
}
function label(id) {
  var e = MANIFEST.seasons.filter(function (s) { return s.id === id; })[0];
  return e ? e.label : id.slice(0, 4) + "-" + id.slice(6);
}
var PARTIAL = {};
function partialSeasons(ids) {
  return MANIFEST.seasons.filter(function (s) { return s.quarantined && ids.indexOf(s.id) >= 0; });
}
function partialText(s) {
  return s.label + ": " + s.games.toLocaleString() + " of " + (s.games + s.quarantined).toLocaleString() + " games verified";
}
function renderMeta() {
  if (multi()) {
    var ids = rangeIds(), part = partialSeasons(ids);
    $("through").textContent = "";
    $("prov").textContent = ids.length + " season" + (ids.length === 1 ? "" : "s") +
      " folded from season totals per player and club (data/player_seasons.json). Every game behind them " +
      "passed the same per-game checks; games that failed are left out of their season." +
      (part.length ? " † marks a player with a partial season in this view. Partial seasons — " +
        part.map(partialText).join("; ") + "." : "");
    $("excluded").innerHTML = "";
    return;
  }
  var last = D.games.length ? D.games[D.games.length - 1].date : "";
  $("through").textContent = last ? " · games through " + niceDate(last) : "";
  var counts = {};
  D.games.forEach(function (g) { var s = D.sources[g.src] || "?"; counts[s] = (counts[s] || 0) + 1; });
  $("prov").textContent = D.games.length.toLocaleString() + " games included. Shift data: " +
    Object.keys(counts).sort().map(function (k) { return counts[k] + " from " + k; }).join(", ") +
    ". Every game is checked against the boxscore's published time on ice and plus-minus before it is counted.";
  var pe = partialSeasons([S.season])[0];
  if (pe) $("prov").textContent = partialText(pe) + ". " + $("prov").textContent;
  if (D.excluded.length) {
    $("excluded").innerHTML = '<details class="excl"><summary>' + D.excluded.length +
      " game" + (D.excluded.length === 1 ? "" : "s") + " excluded (failed verification)</summary><ul>" +
      D.excluded.map(function (e) {
        return "<li>" + esc(e[1] + " " + e[2] + " @ " + e[3]) + " &mdash; " + esc(e[4]) + "</li>";
      }).join("") + "</ul></details>";
  } else $("excluded").innerHTML = '<p class="prov">No games excluded.</p>';
}
function niceDate(iso) {
  var p = iso.split("-"), m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return m[parseInt(p[1], 10) - 1] + " " + parseInt(p[2], 10);
}
function activeSummary() {
  var bits = [];
  if (S.season === "career") bits.push("careers");
  else if (S.season === "range") { var ids = rangeIds(); if (ids.length) bits.push(label(ids[0]) + "–" + label(ids[ids.length - 1])); }
  var o = $("scope").options[$("scope").selectedIndex];
  bits.push(o ? o.textContent : "League");
  if (S.pos) bits.push($("pos").options[$("pos").selectedIndex].textContent);
  if (!multi()) {
    if (S.games === "custom") bits.push((S.from || "start") + " to " + (S.to || "today"));
    else if (S.games) bits.push("last " + S.games);
    if (S.venue) bits.push(S.venue);
    if (S.rookies) bits.push("rookies");
  }
  bits.push("≥" + minToi() + " min");
  if (S.per60) bits.push("per 60");
  if (S.q) bits.push("“" + S.q + "”");
  $("activeSummary").textContent = bits.join(" · ");
}
function refresh() {
  result = multi() ? computeStints() : compute();
  sortRows(result);
  renderHead(); renderBody(); renderTiles(); activeSummary();
  $("count").textContent = result.length.toLocaleString() + " skater" + (result.length === 1 ? "" : "s") +
    (multi() ? " (one row each, clubs combined)" : "") +
    (result.length > PAGE_ROWS && !showAll ? ", top " + PAGE_ROWS + " shown" : "");
  writeUrl();
}

// -- controls ---------------------------------------------------------------
function buildScope() {
  if (multi()) return buildScopeMulti();
  var sel = $("scope"), h = ['<option value="">League</option>'];
  var confs = Object.keys(D.confs).sort(function (a, b) { return D.confs[a].localeCompare(D.confs[b]); });
  h.push('<optgroup label="Conference">' + confs.map(function (c) {
    return '<option value="c:' + esc(c) + '">' + esc(D.confs[c]) + "</option>"; }).join("") + "</optgroup>");
  var divs = Object.keys(D.divs).sort(function (a, b) { return D.divs[a].localeCompare(D.divs[b]); });
  h.push('<optgroup label="Division">' + divs.map(function (c) {
    return '<option value="d:' + esc(c) + '">' + esc(D.divs[c]) + "</option>"; }).join("") + "</optgroup>");
  var teams = D.teams.slice().sort(function (a, b) { return a.name.localeCompare(b.name); });
  h.push('<optgroup label="Team">' + teams.map(function (t) {
    return '<option value="t:' + esc(t.abbr) + '">' + esc(t.name) + "</option>"; }).join("") + "</optgroup>");
  sel.innerHTML = h.join("");
  if (S.scope && !Array.prototype.some.call(sel.options, function (o) { return o.value === S.scope; })) S.scope = "";
  sel.value = S.scope;
}
function buildScopeMulti() {
  var sel = $("scope"), h = ['<option value="">League</option>'];
  var opt = function (prefix, items) {
    return items.map(function (it) { return '<option value="' + prefix + esc(it[0]) + '">' + esc(it[1]) + "</option>"; }).join("");
  };
  var confs = Object.keys(C.confNames).sort(), divs = Object.keys(C.divNames).sort();
  if (confs.length) h.push('<optgroup label="Conference">' + opt("c:", confs.map(function (c) { return [c, c]; })) + "</optgroup>");
  if (divs.length) h.push('<optgroup label="Division (as aligned each season)">' + opt("d:", divs.map(function (d) { return [d, d]; })) + "</optgroup>");
  var teams = Object.keys(C.teamNames).sort(function (a, b) { return C.teamNames[a].localeCompare(C.teamNames[b]); });
  h.push('<optgroup label="Team">' + opt("t:", teams.map(function (t) { return [t, C.teamNames[t] + " (" + t + ")"]; })) + "</optgroup>");
  sel.innerHTML = h.join("");
  if (S.scope && !Array.prototype.some.call(sel.options, function (o) { return o.value === S.scope; })) S.scope = "";
  sel.value = S.scope;
}
function syncControls() {
  buildScope();
  $("pos").value = S.pos; $("games").value = S.games;
  $("dateWrap").hidden = S.games !== "custom";
  var G = D && !multi() ? D.games : [];
  var first = G.length ? G[0].date : "", last = G.length ? G[G.length - 1].date : "";
  ["from", "to"].forEach(function (id) { $(id).min = first; $(id).max = last; });
  $("from").value = S.from; $("to").value = S.to;
  $("min").value = minToi(); $("rookies").checked = S.rookies; $("q").value = S.q;
  setSeg("venue", S.venue); setSeg("mode", S.per60 ? "60" : "");
  var ss = $("season");
  var opts = MANIFEST.seasons.map(function (s) {
    return '<option value="' + esc(s.id) + '">' + esc(s.label) + "</option>"; }).join("");
  if (MANIFEST.seasons.length > 1)
    opts += '<option disabled>──────</option><option value="range">Season range…</option>' +
            '<option value="career">Entire career</option>';
  ss.innerHTML = opts;
  ss.value = S.season;
  var asc = MANIFEST.seasons.slice().reverse().map(function (s) {
    return '<option value="' + esc(s.id) + '">' + esc(s.label) + "</option>"; }).join("");
  $("sfrom").innerHTML = asc; $("sto").innerHTML = asc;
  var ids = rangeIds();
  if (ids.length) { $("sfrom").value = ids[0]; $("sto").value = ids[ids.length - 1]; }
  $("sfrom").hidden = $("sto").hidden = S.season !== "range";
  Array.prototype.forEach.call(document.querySelectorAll(".gonly"), function (el) { el.hidden = multi(); });
  $("gnote").hidden = !multi();
  var entry = MANIFEST.seasons.filter(function (s) { return s.id === S.season; })[0];
  $("seasonLabel").textContent = entry ? entry.label
    : S.season === "career" ? "Careers" : (ids.length ? label(ids[0]) + "–" + label(ids[ids.length - 1]) : "");
}
function setSeg(id, v) {
  Array.prototype.forEach.call($(id).querySelectorAll("button"), function (b) {
    b.setAttribute("aria-pressed", String(b.dataset.v === v));
  });
}
function on(id, ev, fn) { $(id).addEventListener(ev, fn); }
function wire() {
  on("scope", "change", function () {
    S.scope = this.value; showAll = false;
    if (S.min === null) $("min").value = minDefault();
    refresh();
  });
  on("pos", "change", function () { S.pos = this.value; showAll = false; refresh(); });
  on("games", "change", function () {
    S.games = this.value; $("dateWrap").hidden = S.games !== "custom";
    if (S.games !== "custom") { S.from = S.to = ""; $("from").value = $("to").value = ""; }
    showAll = false; refresh();
  });
  on("from", "change", function () { S.from = this.value; refresh(); });
  on("to", "change", function () { S.to = this.value; refresh(); });
  on("min", "input", function () {
    var v = parseFloat(this.value);
    S.min = isNaN(v) ? null : Math.max(0, v);
    refresh();
  });
  on("rookies", "change", function () { S.rookies = this.checked; refresh(); });
  var timer = null;
  on("q", "input", function () {
    var v = this.value; clearTimeout(timer);
    timer = setTimeout(function () { S.q = v; refresh(); }, 140);
  });
  $("venue").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    S.venue = b.dataset.v; setSeg("venue", S.venue); refresh();
  });
  $("mode").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    S.per60 = b.dataset.v === "60"; setSeg("mode", b.dataset.v);
    if (!S.per60 && S.sort === "gd60") S.sort = HEAD.k;
    refresh();
  });
  $("head").addEventListener("click", function (e) {
    var th = e.target.closest("th"); if (!th) return;
    var k = th.dataset.k, col = COLS.filter(function (c) { return c.k === k; })[0];
    if (S.sort === k) S.dir = S.dir === "desc" ? "asc" : "desc";
    else { S.sort = k; S.dir = col.t === "num" ? "desc" : "asc"; }
    refresh();
  });
  on("more", "click", function () { showAll = true; refresh(); });
  on("reset", "click", function () {
    var season = S.season, a = S.sfrom, b = S.sto; S = defaults(); S.season = season; S.sfrom = a; S.sto = b;
    showAll = false; syncControls(); refresh();
  });
  on("season", "change", function () {
    var id = this.value, wasMulti = multi();
    S.season = id;
    if (multi() !== wasMulti) S.scope = "";   // scope codes differ between the two modes
    $("body").innerHTML = '<tr><td class="state" colspan="12">Loading&hellip;</td></tr>';
    showAll = false;
    load(id).then(function () { syncControls(); renderMeta(); refresh(); }).catch(fail);
  });
  ["sfrom", "sto"].forEach(function (id) {
    on(id, "change", function () {
      S.sfrom = $("sfrom").value; S.sto = $("sto").value;
      syncControls(); renderMeta(); refresh();
    });
  });
  on("csv", "click", downloadCsv);
}
function downloadCsv() {
  var cols = ["player", "player_id", "team", "pos", "gp", "toi_5v5", "official_pm", "gd_on",
              "nonclean", "expected", "adjusted", "rel", "gd_on_per_60", "adjusted_per_60"];
  var lines = [cols.join(",")];
  result.forEach(function (r) {
    var adj60 = r.sec ? r.adj * 3600 / r.sec : "";
    lines.push(['"' + r.name.replace(/"/g, '""') + '"', r.pid, r.team, r.pos, r.gp, r.toi.toFixed(2),
      r.pm, r.gd, r.nc, r.exp.toFixed(3), r.adj.toFixed(3), r.rel === null ? "" : r.rel.toFixed(3),
      r.gd60 === null ? "" : r.gd60.toFixed(3), adj60 === "" ? "" : adj60.toFixed(3)].join(","));
  });
  var blob = new Blob([lines.join("\n") + "\n"], {type: "text/csv"});
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  var span = multi() ? (S.season === "career" ? "career" : rangeIds()[0] + "-" + rangeIds().slice(-1)[0]) : S.season;
  a.download = "adjusted_pm_" + (S.scope ? S.scope.slice(2).toLowerCase().replace(/\W+/g, "_") : "league") + "_" + span + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
}
function fail(err) {
  $("body").innerHTML = '<tr><td class="state" colspan="12">Could not load the data (' +
    esc(err && err.message || err) + ").</td></tr>";
}

// Read-only hook for automated checks: the rows behind the current view.
window.apmView = function () {
  return {result: result, state: S, games: multi() ? (C ? C.seasons.length : 0) : (D && D.games.length), mode: multi() ? "stints" : "games"};
};

// Filters start collapsed on a phone, open on a wide screen.
if (window.matchMedia && !window.matchMedia("(min-width: 760px)").matches) $("filters").open = false;
S = readUrl();
getJson("data/index.json", true).then(function (m) {
  MANIFEST = m;
  m.seasons.forEach(function (s) { if (s.quarantined) PARTIAL[s.id] = s; });
  return (multi() ? loadCareers() : load(S.season || m["default"]));
}).then(function () {
  syncControls(); wire(); renderMeta(); refresh();
}).catch(fail);
})();
</script>
</body>
</html>
"""
