"""External betting-market clients — currently just Kalshi (see
`ffbot.markets.kalshi`). Deliberately separate from `ffbot.history`: these
are LIVE market reads, not immutable historical box scores, so none of
`ffbot.history.fetch`'s "cache forever, never re-fetch" contract applies
here. Nothing in this package is wired into any decision path yet — see
`ffbot.markets.kalshi`'s module docstring for why.
"""
