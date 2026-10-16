"""Static configuration for the adjusted plus-minus tool."""

SEASON = "20262027"
TEAM = "CHI"

GAME_TYPE_PRESEASON = 1
GAME_TYPE_REGULAR = 2
COMPLETED_GAME_STATES = ("FINAL", "OFF")

# Every period is addressed as a 20-minute block so shift rows and plays share a
# single absolute-seconds axis. Regular-season OT is shorter than its block, but
# OT is 3v3 and the shootout is not hockey, so the unused tail costs nothing.
PERIOD_BLOCK_SECONDS = 1200
REGULATION_PERIOD_SECONDS = 1200
REGULATION_PERIODS = 3
SHOOTOUT_PERIOD_TYPE = "SO"

# situationCode digits are, in order:
#   away goalies, away skaters, home skaters, home goalies
# Verified against game 2026020098 (PIT @ CHI) -- see README for the evidence.
# "1551" therefore means 5 skaters a side with both goalies in net.
EVEN_STRENGTH_5V5 = "1551"

# The shiftcharts feed mixes goal rows in with shift rows; only this type code is
# an actual shift.
SHIFT_ROW_TYPE_CODE = 517

GOALIE_POSITION_CODE = "G"

BASE_API_WEB = "https://api-web.nhle.com/v1"
BASE_API_STATS = "https://api.nhle.com/stats/rest/en"

# A game whose shift feed yields less clean 5v5 time than this is reported as
# suspicious rather than silently averaged in. Typical NHL games land near 40.
MIN_PLAUSIBLE_5V5_MINUTES = 20.0

BASE_HTML_REPORTS = "https://www.nhl.com/scores/htmlreports"

# Fallback shift source. The HTML Time On Ice reports ("TV" = visitor, "TH" =
# home) carry per-shift start/end times for games the JSON shiftcharts feed
# returns empty. Periods appear as digits; OT/SO appear by name.
HTML_PERIOD_ALIASES = {"OT": 4, "SO": 5}

SOURCE_SHIFTCHARTS = "shiftcharts"
SOURCE_HTML_TOI = "html-toi"

# -- site build -------------------------------------------------------------
# The site covers every club. The processed data lives in one file per season
# under docs/data/, so an older season is a second file, not a schema change.
PRIMARY_CSV_NAME = "hawks_adjusted_pm.csv"
DOCS_DIR = "docs"
DATA_DIR = "data"               # relative to DOCS_DIR
SITE_TITLE = "NHL Adjusted Plus-Minus"
DISPLAY_TIMEZONE = "America/Chicago"
DEFAULT_PAGE_TEAM = "CHI"       # the team the CLI defaults to

# -- league-scale publish gate ----------------------------------------------
# A game that fails verification is quarantined, not fatal. Publishing is
# blocked only for systemic trouble: more quarantined games than
# max(MAX_QUARANTINE_GAMES, MAX_QUARANTINE_SHARE x completed games), or a
# league-level identity breaking. The floor keeps a handful of bad games early
# in a season (when 2% is one or two games) from blocking the site.
MAX_QUARANTINE_SHARE = 0.02
MAX_QUARANTINE_GAMES = 5


def quarantine_limit(completed: int) -> int:
    """Most quarantined games a season may have and still publish."""
    return max(MAX_QUARANTINE_GAMES, int(MAX_QUARANTINE_SHARE * completed))
# Float tolerance for the zero-sum identities (they are exact in integers;
# only the Expected division introduces rounding).
ZERO_SUM_TOLERANCE = 1e-6

# Polarity colors for the Adjusted column. Green/red is the hockey convention
# and is what ships, but it measures deutan Delta E 4.1 -- below the usable
# floor -- so hue is never the only cue: the signed numeral and the bar's
# direction both carry the sign. Swap in the validated diverging pair
# ("#2a78d6" positive / "#d03b3b" negative, deutan Delta E 23.8) here to make
# the page colorblind-safe on hue alone.
ACCENT_POSITIVE = "#0ca30c"
ACCENT_NEGATIVE = "#d03b3b"
