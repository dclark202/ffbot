"""External betting-market clients — currently just Kalshi (see
`ffbot.markets.kalshi`/`ffbot.markets.kalshi_nfl`). Deliberately separate
from `ffbot.history`: these are LIVE market reads, not immutable historical
box scores, so none of `ffbot.history.fetch`'s "cache forever, never
re-fetch" contract applies here. Wired into two live decision paths as of
commit a9c2866 — game-level odds (live at every spice level, feeding
`ffbot.live.conditions`) and per-player props (`SeasonConfig.kalshi_weight`/
`DraftConfig.kalshi_weight`, B7 spice level 4 only) — see
`ffbot.markets.kalshi`'s module docstring for the evidence caveat on the
latter.
"""
