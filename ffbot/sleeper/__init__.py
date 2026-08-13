"""Sleeper (api.sleeper.app / api.sleeper.com) league client — the read-only,
unauthenticated replacement for `ffbot/auth.py`'s Yahoo OAuth2 layer.

Sleeper's public API needs no credentials at all, so this package has no
token/session/rotation machinery to speak of — it is a thin, cache-first HTTP
client, the league-data analog of `ffbot/projections/` (which stays a
sibling, not a merge target: that package is specifically a *projections
provider* in the `ffbot.projections.ProjectionProvider` shape, this one is
the general league client several things build on).

See `client.py` for the endpoints and `models.py` for translating Sleeper's
`injury_status` strings onto this repo's existing status vocabulary (its
flex-slot names are registered directly in `ffbot.models.SLOT_ELIGIBILITY`,
no translation needed). `cache.py` mirrors `ffbot/projections/cache.py`'s
TTL-sidecar pattern rather than `ffbot/history/fetch.py`'s immutable-source
cache — league state changes constantly, so a cache hit must expire.
"""

from __future__ import annotations
