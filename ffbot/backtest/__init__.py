"""B3 — the lineup replayer and its baselines (see docs/BACKTEST.md).

`ffbot.history` is the point-in-time DATA layer, with the leakage guarantee
(`as_of` never fetches a results-bearing source). `ffbot.backtest` is the
EVALUATION layer built on top of it: everything here may compare a decision
against real outcomes, which `ffbot.history` deliberately cannot do.

Design invariant: **code in this package may read ground truth only through
`ffbot.history.actuals.week_actuals`, never `WeekSnapshot.game_rows`
directly.** `game_rows` is `ffbot.history.index`'s own separate
post-hoc-scoring channel (see its docstring); routing every grading read
through one function is what keeps "did we accidentally let a decision see
the answer" a one-place-to-check question instead of an every-call-site one.
"""

from __future__ import annotations
