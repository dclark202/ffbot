"""The live feed for the five form dials, from Sleeper's own realized stats.

`usage_weight`, `momentum_weight`, `divergence_weight`, `volatility_weight`
and `upside_lean_weight` are classed **Validated** in docs/dev/SPICE.md and
were, until this module, structurally inert in production: the only thing
that ever wrote the `WeeklyPlayerIntel` fields they read was a researched
`weekly/week-NN.yml` entry, and `.claude/commands/research-week.md` never
asked for any of them. `_momentum_multiplier` returned exactly 1.0 for
every player, every run, for an entire season. This is the wire.

WHY SLEEPER RATHER THAN NFLVERSE. `ffbot/history/signals.py` feeds the same
math from nflverse `stats_player_week`, and that is the feed the validation
was measured on, so nflverse is the more literal transfer. It is the wrong
choice here anyway:

1. Its in-season publication latency is unverified, and a Tuesday check
   that silently finds no data is the exact failure this module exists to
   end. Sleeper's weekly stats are the same endpoint, cache and failure
   mode as every other live seam in this repo, already warm.
2. The points half is scored by the LEAGUE'S OWN `scoring_settings` through
   `scoring.score_sleeper_stats`, not by a `StatLine` approximation -- the
   same rule that governs every displayed projection here. For `momentum`
   and `volatility`/`upside`, which are functions of league-scored points,
   that makes this feed strictly more faithful to the decision than the
   historical one is.
3. It adds no new dependency to the live path, and `ffbot/history/` stays
   out of `ffbot/report.py` -- no new route from the backtest's fetch layer
   into a live run.

The math itself is shared, not reimplemented: both feeds call
`ffbot/form.py`. The one genuine difference is the usage half. nflverse
publishes WOPR precomputed; here it is derived from Sleeper's per-player
`rec_tgt` and `rec_air_yd` against team totals summed from the same rows.
Same definition (1.5 x target share + 0.7 x air-yards share), computed a
step earlier. `tests/test_live_form.py` pins the formula.

THREE COMPLETED GAMES ARE REQUIRED before any of this says anything
(`form.MIN_GAMES`), so all five dials stay silent through week 3 of a
season and begin speaking in week 4. A run before then is not broken and
the coverage alert says so in those words rather than leaving it to be
rediscovered.

Every fetch failure degrades to "no signal this run" with a surfaced alert,
never a crash and never a silent success -- if this went quiet the way the
research feed did, nothing would tell you.
"""

from __future__ import annotations

from collections import defaultdict

from .. import form as form_math
from ..config import Config, LeagueScoring
from ..names import normalize_name
from ..scoring import score_sleeper_stats

# WOPR, as nflverse defines it and as `history/signals.py` consumes it.
_WOPR_TARGET_SHARE = 1.5
_WOPR_AIR_YARDS_SHARE = 0.7


def _key(name: str, position: str) -> str:
    return f"{normalize_name(name)}:{position.upper()}"


def _identity(row: dict) -> tuple[str, str, str]:
    """`(name, position, team)` from a row as
    `projections.sleeper.fetch_actual_weekly_rows` returns it.

    That normalized shape rather than the raw endpoint JSON on purpose: it
    has already applied this repo's position filter and name/team
    translation at the boundary, so this module never has to repeat a
    decision the projections layer has already made.
    """
    return (
        (row.get("name") or "").strip(),
        (row.get("position") or "").strip().upper(),
        (row.get("team") or "").strip().upper(),
    )


def build_logs(
    weekly_rows: dict[int, list[dict]], scoring: LeagueScoring,
) -> tuple[dict, dict]:
    """`(points_log, usage_log)` in `ffbot.form`'s `{key: [(week, value)]}`
    shape, from `{week: rows}` of realized Sleeper stats.

    The points side is scored by the LEAGUE'S OWN `scoring_settings` rather
    than by the row's `pts_ppr`: `momentum` and `volatility` are supposed to
    describe what a player did for THIS team, and a PPR total is a different
    number in a league that pays for first downs.

    Team shares are computed per week from that week's rows only, so a
    player's target share is measured against the offense he actually played
    in rather than against a season aggregate a trade or an injury has made
    meaningless.
    """
    settings = scoring_settings_of(scoring)
    points_log: dict[str, list[tuple[int, float]]] = defaultdict(list)
    usage_log: dict[str, list[tuple[int, float]]] = defaultdict(list)

    for week, rows in sorted(weekly_rows.items()):
        team_targets: dict[str, float] = defaultdict(float)
        team_air_yards: dict[str, float] = defaultdict(float)
        for row in rows:
            _name, position, team = _identity(row)
            if position not in form_math.USAGE_POSITIONS or not team:
                continue
            stats = row.get("sleeper_stats") or {}
            team_targets[team] += float(stats.get("rec_tgt") or 0.0)
            team_air_yards[team] += float(stats.get("rec_air_yd") or 0.0)

        for row in rows:
            name, position, team = _identity(row)
            if not name or not position:
                continue
            stats = row.get("sleeper_stats") or {}
            key = _key(name, position)
            points_log[key].append((week, score_sleeper_stats(stats, settings)))

            if position not in form_math.USAGE_POSITIONS or not team:
                continue
            tgt_total = team_targets.get(team, 0.0)
            air_total = team_air_yards.get(team, 0.0)
            if tgt_total <= 0:
                continue  # no passing game that week says nothing about share
            target_share = float(stats.get("rec_tgt") or 0.0) / tgt_total
            air_share = (
                float(stats.get("rec_air_yd") or 0.0) / air_total if air_total > 0 else 0.0
            )
            usage_log[key].append(
                (week, _WOPR_TARGET_SHARE * target_share + _WOPR_AIR_YARDS_SHARE * air_share)
            )

    return dict(points_log), dict(usage_log)


def scoring_settings_of(scoring: LeagueScoring | dict | None) -> dict:
    """`score_sleeper_stats` wants Sleeper's own `scoring_settings` dict.

    A `LeagueScoring` that carries one (the live league's, or `league.yml`'s
    offline copy) hands it over; anything else yields `{}`, which scores
    every player at zero and is therefore treated as no coverage rather than
    as a season of zeroes -- see `live_form_signals`' guard.
    """
    if isinstance(scoring, dict):
        return scoring
    return getattr(scoring, "sleeper_scoring_settings", None) or {}


def live_form_signals(
    season: int,
    week: int,
    cfg: Config,
    fetch_week,
    min_games: int = form_math.MIN_GAMES,
    recent_games: int = form_math.RECENT_GAMES,
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """`({normalized name: {signal: 0..100}}, alerts)` for weeks `< week`.

    `fetch_week(week) -> rows` is injected rather than imported so the
    network stays testable and this module has no opinion about caching --
    `report.load_everything` passes `projections.fetch_actual_weekly_rows`
    bound to the run's cache directory and TTL.

    LEAKAGE: only weeks strictly BEFORE `week` are read, the same boundary
    `history.signals` enforces. A signal computed from the week it is
    predicting would be worthless and would quietly flatter every grade.

    Partial failure is partial: a week that will not fetch is skipped with
    an alert and the remaining weeks still produce signals, because three
    good weeks out of four is a real signal and refusing it would be worse.
    """
    scoring = cfg.league or LeagueScoring.fantasypros_default()
    settings = scoring_settings_of(scoring)
    alerts: list[str] = []

    if not settings:
        alerts.append(
            "Form signals (sleeper) have no league scoring settings this run — "
            "usage/momentum/divergence/volatility/upside stay inert rather than "
            "scoring every player at zero."
        )
        return {}, alerts

    weekly_rows: dict[int, list[dict]] = {}
    for wk in range(1, max(1, week)):
        try:
            rows = fetch_week(wk)
        except Exception as exc:  # noqa: BLE001 -- any fetch failure is one skipped week
            alerts.append(
                f"Form signals (sleeper) could not read week {wk} this run ({exc}) — "
                "that week is excluded from every form signal."
            )
            continue
        if rows:
            weekly_rows[wk] = rows

    if not weekly_rows:
        return {}, alerts

    points_log, usage_log = build_logs(weekly_rows, scoring)
    merged: dict[str, dict[str, float]] = defaultdict(dict)
    for produced in (
        form_math.variance_scores(points_log, min_games=min_games),
        form_math.scoring_trend_scores(points_log, min_games=min_games, recent_games=recent_games),
        form_math.usage_trend_scores(usage_log, min_games=min_games, recent_games=recent_games),
        form_math.divergence_scores(
            usage_log, points_log, min_games=min_games, recent_games=recent_games,
        ),
    ):
        for name, scores in produced.items():
            merged[name].update(scores)

    if not merged and len(weekly_rows) < min_games:
        alerts.append(
            f"Form signals (sleeper): {len(weekly_rows)} completed week(s) so far, "
            f"{min_games} needed — usage/momentum/divergence/volatility/upside are "
            "silent until week " + str(min_games + 1) + ", by design."
        )
    return dict(merged), alerts
