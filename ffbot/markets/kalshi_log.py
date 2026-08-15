"""Forward-logging for Kalshi's per-player prop signal — the honest next
step `ffbot.markets.kalshi`'s own docstring has always pointed at and B7
finally builds (see docs/dev/BACKTEST.md's B7 section / docs/dev/SPICE.md).

Kalshi's NFL player-prop markets launched September 2025, after this repo's
entire 2021-2024 backtest window — nothing prop-derived can be graded
retrospectively (`ffbot.history.projections.ecr_projections`'s own
leakage-guard reasoning applies just as much to "we have no data to check
against" as to "we refuse to peek at the answer"). The fix isn't
retrospective, it's prospective: log what the market said BEFORE each
week's games, alongside the shipped projection, and grade it against
`ffbot.history.actuals.week_actuals` once a real season's worth has
accumulated. That's real evidence in about seventeen weeks, at zero
marginal cost today — this module deliberately piggybacks on the fetch
`ffbot.report.load_everything` already makes when `SeasonConfig.kalshi_weight
!= 0.0` (the existing "don't even ask" no-network guard — see that
function's own kalshi block) rather than adding a new one, so logging never
costs a user who hasn't turned Kalshi on anything at all, not even a disk
write.

Deliberately NOT under `ffbot/history/` — this is live, forward-looking
data collection, not point-in-time historical replay; none of
`ffbot.history.fetch`'s "cache forever, immutable past" contract applies.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Optional

DEFAULT_LOG_DIR = Path("data/kalshi_log")


def log_weekly_snapshot(
    season: int,
    week: int,
    player_prop_scores: dict[str, float],
    game_odds: Optional[dict[str, dict[str, float]]] = None,
    log_dir: Path | str = DEFAULT_LOG_DIR,
    now: Optional[_dt.datetime] = None,
) -> Optional[Path]:
    """Append one JSON line — `{season, week, logged_at, player_prop_scores,
    game_odds}` — to `<log_dir>/<season>.jsonl`.

    `player_prop_scores` is `{board_key: 0..1}`, exactly `ffbot.markets
    .kalshi_nfl.weekly_signal`'s own return shape — the untested signal this
    whole module exists to eventually grade. `game_odds` (optional) is
    `{team: {"team_total": ..., "opp_total": ...}}`, already-fetched context
    from the SAME weekly report run (`WeeklyIntel.games`, itself fed by
    `ffbot.live.conditions` at every spice level) — recorded for context, not
    a second fetch; the game-level odds path already has a backtestable
    proxy via `games.csv`'s own spread/total lines, so it isn't the thing
    this module was built to close the gap on.

    Returns `None` (nothing written) when `player_prop_scores` is empty —
    either a fetch failure (already alerted separately by the caller) or a
    week with no matched games; there is nothing worth logging either way.
    Never raises: a logging failure must not take down a weekly report that
    has, by the time this runs, already succeeded. `now` is injectable for
    deterministic tests; real callers leave it `None` (`datetime.now(UTC)`).
    """
    if not player_prop_scores:
        return None
    log_dir = Path(log_dir)
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{season}.jsonl"
        row = {
            "season": season,
            "week": week,
            "logged_at": (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(),
            "player_prop_scores": player_prop_scores,
            "game_odds": game_odds or {},
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return path
    except OSError:
        return None
