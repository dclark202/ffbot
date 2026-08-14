"""NFL semantics on top of `ffbot.markets.kalshi`'s raw, series-agnostic
client. Two independent capabilities, sharing the same event-to-game
matching and liquidity-gating machinery:

1. **Game-level odds** (`game_odds`) — implied team totals from
   `KXNFLTOTAL`/`KXNFLSPREAD`'s public ladders. Feeds
   `ffbot.live.conditions`' `GameInfo.team_total`/`opp_total`, live at
   EVERY spice level — a market-implied total is no more "speculative" than
   a Vegas total typed in by hand during `/gameday`; see
   `config.yml`'s `game_conditions` block.
2. **Per-player prop probabilities** (`player_prop_probabilities`) — single
   yes/no markets ("Player X: 1+ touchdowns"), the raw material for the
   spice-level-4-only per-player signal (see `ffbot/week.py`'s
   `kalshi_score` and `ffbot/edge.py` — Phase 4). This module only fetches
   and parses; turning a probability into a decision-facing 0-100 score is
   decision-layer work, not a market-reading concern.

Every market is a KX-style binary contract on a threshold ("over N.5
points", "wins by over N.5", "1+ touchdowns") — never a single number Kalshi
just hands over. `_implied_median` extracts a line from a LADDER of such
contracts the same way a sportsbook's own price-discovery does: the
threshold where the market's own mid-price crosses 50%, linearly
interpolated between the two straddling contracts.

Matching a Kalshi event to one of this week's real games is done via each
event's `sub_title` ("DET vs CIN (Aug 13)" — confirmed live during
scoping), not by parsing the ticker string (team-code lengths are
ambiguous — "GB"/"NE" are 2 letters, "DET"/"CIN" are 3, with no separator).
An event that can't be matched to a game already in `ffbot.live.schedule`'s
output is silently skipped — an unmatched game is safer than a wrong one,
the same philosophy `ffbot.names` documents for player-name matching.

Liquidity gating throughout (`_liquid_enough`): a wide bid/ask spread or no
trading volume is noise, not signal — verified live during scoping (a
preseason KXNFLTD market showed a 0.12/0.63 bid/ask spread, unusable as a
probability estimate).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Optional, Sequence

from ..history.names import canonical_team
from ..live.schedule import LiveGame
from ..names import normalize_name
from . import kalshi
from .kalshi import UrlOpener

# A market's price is trustworthy only inside this spread and above this
# activity floor. Both are conservative on purpose -- a signal this module
# silently drops is far cheaper than one that quietly feeds noise into a
# recommendation. Dollars, matching Kalshi's own `*_dollars` fields.
_MAX_SPREAD_DOLLARS = 0.15
_MIN_ACTIVITY = 1.0  # volume_fp or open_interest_fp, whichever is higher

# City/nickname fragments used to disambiguate WHICH of a game's two teams
# a Kalshi spread market's `yes_sub_title` names (e.g. "Seattle wins by
# over 7.5 points"). Ordered most-specific first so a same-metro pair
# (NYG/NYJ, LAR/LAC) disambiguates on nickname before falling back to a
# shared city name that would otherwise match both candidates at once.
_TEAM_NAME_HINTS: dict[str, tuple[str, ...]] = {
    "ARI": ("Arizona", "Cardinals"), "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"), "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"), "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"), "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"), "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"), "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"), "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars"), "KC": ("Kansas City", "Chiefs"),
    "LAC": ("Chargers", "Los Angeles"), "LAR": ("Rams", "Los Angeles"),
    "LV": ("Las Vegas", "Raiders"), "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"), "NE": ("New England", "Patriots"),
    "NO": ("New Orleans", "Saints"), "NYG": ("Giants", "New York"),
    "NYJ": ("Jets", "New York"), "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"), "SEA": ("Seattle", "Seahawks"),
    "SF": ("San Francisco", "49ers"), "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"), "WAS": ("Washington", "Commanders"),
}

_SUB_TITLE_RE = re.compile(r"^([A-Za-z]{2,3})\s+vs\s+([A-Za-z]{2,3})", re.IGNORECASE)


def _dollar(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _mid_price(market: dict) -> Optional[float]:
    """Mid of the resting bid/ask, falling back to the last trade price when
    one side of the book is empty (0.0 or missing at the edges of a thin
    market) -- the same fallback `ffbot.markets.kalshi`'s own docstring
    anticipates for a market with real but one-sided interest."""
    bid = _dollar(market.get("yes_bid_dollars"))
    ask = _dollar(market.get("yes_ask_dollars"))
    if bid is not None and ask is not None and ask > 0:
        return (bid + ask) / 2.0
    return _dollar(market.get("last_price_dollars"))


def _liquid_enough(market: dict) -> bool:
    bid = _dollar(market.get("yes_bid_dollars"))
    ask = _dollar(market.get("yes_ask_dollars"))
    if bid is None or ask is None or ask < bid:
        return False
    if ask - bid > _MAX_SPREAD_DOLLARS:
        return False
    activity = max(_dollar(market.get("volume_fp")) or 0.0, _dollar(market.get("open_interest_fp")) or 0.0)
    return activity >= _MIN_ACTIVITY


def _implied_median(markets: list[dict]) -> Optional[float]:
    """The threshold where a ladder of "over X" binary markets' mid-price
    crosses 50%, linearly interpolated between the two straddling
    contracts -- the market's own implied median line. `None` if fewer than
    two markets pass liquidity gating (too thin to trust)."""
    points: list[tuple[float, float]] = []
    for m in markets:
        if not _liquid_enough(m):
            continue
        strike = _dollar(m.get("floor_strike"))
        mid = _mid_price(m)
        if strike is None or mid is None:
            continue
        points.append((strike, mid))
    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    for (s0, p0), (s1, p1) in zip(points, points[1:]):
        if p0 >= 0.5 >= p1 and p1 != p0:
            frac = (p0 - 0.5) / (p0 - p1)
            return s0 + frac * (s1 - s0)
    # No crossing (every price sits on one side of 50%, e.g. a lopsided
    # blowout total) -- the strike whose price is nearest 50% is the best
    # available read.
    closest = min(points, key=lambda p: abs(p[1] - 0.5))
    return closest[0]


def _event_teams(sub_title: str) -> Optional[tuple[str, str]]:
    """Parse a Kalshi event's `sub_title` ("DET vs CIN (Aug 13)") into
    `(away, home)` abbreviations, `canonical_team`-normalized so a Kalshi
    code that differs from nflverse's own still resolves onto the key every
    other live seam uses."""
    m = _SUB_TITLE_RE.match(sub_title.strip())
    if not m:
        return None
    away = canonical_team(m.group(1))
    home = canonical_team(m.group(2))
    if not away or not home:
        return None
    return away, home


def _identify_spread_team(text: str, candidates: tuple[str, str]) -> Optional[str]:
    """Which of `candidates` a spread market's `yes_sub_title` names.
    Unambiguous only when exactly one candidate's hints appear -- a
    same-metro pair sharing a city name (NYG/NYJ, LAR/LAC) correctly
    resolves to `None` unless the text also carries a nickname."""
    text_lower = text.lower()
    matches = [
        abbr for abbr in candidates
        if any(hint.lower() in text_lower for hint in _TEAM_NAME_HINTS.get(abbr, ()))
    ]
    return matches[0] if len(matches) == 1 else None


def _games_by_teams(games: dict[str, LiveGame]) -> dict[tuple[str, str], tuple[str, str]]:
    """`{(away, home): (away, home)}` -- one entry per real game (not per
    team-keyed row), derived from `ffbot.live.schedule.this_week_games`'s
    output. A plain dict rather than a set so the return value can double
    as a membership+identity lookup in one step."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for team, game in games.items():
        if game.home:
            out[(game.opponent, team)] = (game.opponent, team)
    return out


def game_odds(
    games: dict[str, LiveGame],
    series: Sequence[str] = ("KXNFLTOTAL", "KXNFLSPREAD"),
    opener: UrlOpener = kalshi._default_opener,
) -> dict[str, dict[str, float]]:
    """`{team: {"team_total": .., "opp_total": ..}}` derived from Kalshi's
    public total/spread ladders, for every game in `games` Kalshi has an
    open, liquid market for. `series` is the allowlist (see
    `config.yml`'s `game_conditions.kalshi_odds_series`) -- passing a
    series this module doesn't understand yet is silently a no-op for that
    series, not an error (see the module docstring's note on allowlist vs.
    hardcoded set).

    A team's total is populated only when BOTH a total line AND an
    identifiable spread line are available -- deliberately never a
    fabricated 50/50 split of an unmatched spread; `GameInfo.team_total`
    staying `None` is already a well-handled "no data" state everywhere
    downstream (`week.vegas_multiplier` returns exactly 1.0), so a
    half-guessed number would be strictly worse than that.
    """
    wanted = _games_by_teams(games)
    if not wanted:
        return {}

    totals: dict[tuple[str, str], float] = {}
    spreads: dict[tuple[str, str], tuple[str, float]] = {}  # (away,home) -> (favored_team, margin)

    if "KXNFLTOTAL" in series:
        try:
            events = kalshi.list_events("KXNFLTOTAL", status="open", opener=opener)
        except kalshi.KalshiError:
            events = []
        for event in events:
            pair = _event_teams(event.get("sub_title") or "")
            if pair is None or pair not in wanted:
                continue
            try:
                markets = kalshi.list_markets(event_ticker=event.get("event_ticker"), status="open", opener=opener)
            except kalshi.KalshiError:
                continue
            total = _implied_median(markets)
            if total is not None:
                totals[pair] = total

    if "KXNFLSPREAD" in series:
        try:
            events = kalshi.list_events("KXNFLSPREAD", status="open", opener=opener)
        except kalshi.KalshiError:
            events = []
        for event in events:
            pair = _event_teams(event.get("sub_title") or "")
            if pair is None or pair not in wanted:
                continue
            try:
                markets = kalshi.list_markets(event_ticker=event.get("event_ticker"), status="open", opener=opener)
            except kalshi.KalshiError:
                continue
            liquid = [m for m in markets if _liquid_enough(m)]
            if not liquid:
                continue
            favored = _identify_spread_team(liquid[0].get("yes_sub_title") or "", pair)
            if favored is None:
                continue
            margin = _implied_median(markets)
            if margin is not None:
                spreads[pair] = (favored, margin)

    out: dict[str, dict[str, float]] = {}
    for pair, (away, home) in wanted.items():
        total = totals.get(pair)
        if total is None:
            continue
        favored_margin = spreads.get(pair)
        if favored_margin is None:
            continue
        favored, margin = favored_margin
        home_margin = margin if favored == home else -margin
        home_total = total / 2.0 + home_margin / 2.0
        away_total = total / 2.0 - home_margin / 2.0
        out[home] = {"team_total": home_total, "opp_total": away_total}
        out[away] = {"team_total": away_total, "opp_total": home_total}

    return out


def season_player_probabilities(
    series_ticker: str,
    opener: UrlOpener = kalshi._default_opener,
) -> dict[str, float]:
    """`{sleeper_display_name: implied_probability}` (0..1) for every liquid
    market under a SEASON-LONG series (e.g. `"KXNFLFFTOP"` -- "Top Fantasy
    Rank" -- or `"KXNFLSEASONRSHYDS"`). Unlike `game_odds`/
    `player_prop_probabilities`, no event-to-game matching is needed: a
    season-long series has ONE event for the whole season, not one per
    game, so every liquid market under it is already in scope.

    Same never-raise, empty-on-failure contract as
    `player_prop_probabilities` — a wholesale series failure degrades to
    "no season props available," not a crash.
    """
    try:
        markets = kalshi.list_markets(series_ticker=series_ticker, status="open", opener=opener)
    except kalshi.KalshiError:
        return {}

    out: dict[str, float] = {}
    for market in markets:
        if not _liquid_enough(market):
            continue
        sub_title = market.get("yes_sub_title") or ""
        name = sub_title.split(":")[0].strip()
        if not name:
            continue
        price = _mid_price(market)
        if price is None:
            continue
        out[name] = price
    return out


def rank_within_position(
    probabilities: dict[str, float],
    position_by_key: dict[str, str],
    name_to_key: dict[str, str],
) -> dict[str, float]:
    """`{board_key: probability}` -> `{board_key: 0..1 percentile rank
    within that key's position}` -- the same technique
    `ffbot.edge.volatility_map` uses for cross-site ADP disagreement,
    applied to a market-implied probability instead. Percentile, not the
    raw probability, because a rushing-yards market and a receptions
    market aren't on the same scale, and a position's own baseline
    (a WR's anytime-TD rate looks nothing like a TE's) makes a raw number
    hard to compare across positions.

    `name_to_key` matches Kalshi's own display names onto board keys via a
    normalized-name exact lookup ONLY (`names.normalize_name`) -- no fuzzy
    cascade. A missed match silently drops that one player rather than
    guessing; see the module docstring's "unmatched is safer than wrong"
    reasoning. Callers build `name_to_key` once per board
    (`{normalize_name(bp.name): bp.key for bp in board.players}`).
    """
    by_key: dict[str, float] = {}
    for name, prob in probabilities.items():
        key = name_to_key.get(normalize_name(name))
        if key is not None:
            by_key[key] = prob

    by_pos: dict[str, list[str]] = defaultdict(list)
    for key in by_key:
        pos = position_by_key.get(key)
        if pos is not None:
            by_pos[pos].append(key)

    out: dict[str, float] = {}
    for keys in by_pos.values():
        if len(keys) == 1:
            out[keys[0]] = 0.5
            continue
        ordered = sorted(keys, key=lambda k: by_key[k])
        n = len(ordered)
        for i, key in enumerate(ordered):
            out[key] = i / (n - 1)
    return out


def draft_signal(
    board,
    series_ticker: str = "KXNFLFFTOP",
    opener: UrlOpener = kalshi._default_opener,
) -> dict[str, float]:
    """`{board_key: 0..1}` -- the draft-side Kalshi signal: percentile rank
    of `series_ticker`'s (default `"KXNFLFFTOP"`, "Top Fantasy Rank") market
    probability within position, matched onto `board` by name. Thin
    convenience wrapper combining `season_player_probabilities` +
    `rank_within_position`, since every draft-time caller needs exactly
    this composition (see `ffbot.edge.EdgeContext.kalshi` /
    `DraftConfig.kalshi_weight`). Never raises -- an empty dict on any
    failure, same contract as its two component functions.
    """
    raw = season_player_probabilities(series_ticker, opener=opener)
    if not raw:
        return {}
    name_to_key = {normalize_name(bp.name): bp.key for bp in board.players}
    position_by_key = {bp.key: bp.position for bp in board.players}
    return rank_within_position(raw, position_by_key, name_to_key)


def weekly_signal(
    games: dict[str, LiveGame],
    board,
    series_ticker: str = "KXNFLTD",
    opener: UrlOpener = kalshi._default_opener,
) -> dict[str, float]:
    """`{board_key: 0..1}` -- the weekly-side Kalshi signal: percentile rank
    of `series_ticker`'s (default `"KXNFLTD"`, anytime touchdown -- RB/WR/TE
    only, since Kalshi's own market rules exclude passing TDs) market
    probability within position, matched onto `board` by name. The
    per-game sibling of `draft_signal`, same composition
    (`player_prop_probabilities` + `rank_within_position`) with the
    per-game vs. season-long fetch swapped underneath. Never raises -- an
    empty dict on any failure.
    """
    raw = player_prop_probabilities(series_ticker, games, opener=opener)
    if not raw:
        return {}
    name_to_key = {normalize_name(bp.name): bp.key for bp in board.players}
    position_by_key = {bp.key: bp.position for bp in board.players}
    return rank_within_position(raw, position_by_key, name_to_key)


def player_prop_probabilities(
    series_ticker: str,
    games: dict[str, LiveGame],
    opener: UrlOpener = kalshi._default_opener,
) -> dict[str, float]:
    """`{sleeper_display_name: implied_probability}` (0..1) for every
    liquid market under `series_ticker` (e.g. `"KXNFLTD"`) whose event
    matches one of this week's games. Names are Kalshi's own display text
    (the part of `yes_sub_title` before the threshold, e.g.
    `"Mike Washington Jr."` from `"Mike Washington Jr.: 1+"`) -- matching
    them onto a roster/board is the caller's job (`ffbot.names.
    normalize_name`'s cascade, the same as every other external-name
    source in this repo), not this module's.

    Returns `{}` (never raises) on a wholesale series failure -- callers
    treat that exactly like "no props available this week", the same
    degrade-quietly contract `ffbot.sleeper_roster.apply_sleeper_identity`'s
    ownership fetch already uses.
    """
    wanted = _games_by_teams(games)
    if not wanted:
        return {}

    try:
        events = kalshi.list_events(series_ticker, status="open", opener=opener)
    except kalshi.KalshiError:
        return {}

    out: dict[str, float] = {}
    for event in events:
        pair = _event_teams(event.get("sub_title") or "")
        if pair is None or pair not in wanted:
            continue
        try:
            markets = kalshi.list_markets(event_ticker=event.get("event_ticker"), status="open", opener=opener)
        except kalshi.KalshiError:
            continue
        for market in markets:
            if not _liquid_enough(market):
                continue
            sub_title = market.get("yes_sub_title") or ""
            name = sub_title.split(":")[0].strip()
            if not name:
                continue
            price = _mid_price(market)
            if price is None:
                continue
            out[name] = price

    return out
