"""Auto-fetched, live-only game conditions — the unattended-run analog of
`/gameday`'s hand-researched `weekly/week-NN.yml`. See `ffbot.live.conditions`
for the seam that merges these UNDER a human's own research (which always
wins), and CLAUDE.md's live-seam contract (injectable opener, graceful
degradation, never a crash) that every function here follows.

Deliberately separate from `ffbot.history`: `ffbot.history.index.as_of()`'s
leakage guarantee is about REPLAYING a past week without seeing its outcome;
nothing here replays anything -- this package reads today's real schedule
and forecasts for a week that hasn't happened yet, so no leakage question
applies. `ffbot.live.schedule` does reuse `ffbot.history.fetch`'s cached
downloader for nflverse's `games.csv` (the same schedule file, just fetched
with `refresh=True` since a live in-progress week can still change), but
that is infrastructure reuse, not a second replay path.
"""
