"""Historical NFL data — the backtest data layer.

See docs/dev/BACKTEST.md for the full design. This package turns real past
seasons (nflverse box scores/schedules, DynastyProcess's FantasyPros ECR
archive) into the same shapes `ffbot/week.py` and `ffbot/board.py` already
consume, so the pure optimizer/edge/spice functions can be replayed against
history unchanged.

Design invariant (see CLAUDE.md): all historical replay goes through
`ffbot.history.index.as_of()` — nothing outside this package reads a
historical data file directly, so the point-in-time boundary (no
post-kickoff leakage) is enforced in exactly one place.
"""
