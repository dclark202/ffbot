"""Sleeper HTTP endpoints — stdlib `urllib` only, no auth, matching
`ffbot/history/fetch.py` and `ffbot/projections/sleeper.py`'s existing
stdlib-only convention. Every function takes `cache_dir`/`opener`/`now`, so
every test in `tests/test_sleeper_client.py` runs with zero network access —
same pattern as `tests/test_history_fetch.py`.

Two tiers, kept structurally separate because they carry a different risk:

- **Documented** (`docs.sleeper.com`) — state, user, leagues, league,
  rosters, users, matchups, transactions, traded picks, drafts, draft picks,
  the players dump, trending. Stable; safe to depend on directly.
- **Undocumented** (verified live during scoping, not in Sleeper's docs, and
  explicitly called out by the community as unsupported and subject to
  change without notice) — season projections/ADP
  (`api.sleeper.com/projections/nfl/<season>`) and ownership%
  (`api.sleeper.com/players/nfl/research/...`). Every caller of these MUST
  treat a `SleeperFetchError` as "no data this run," never let it propagate —
  see `ffbot/projections/__init__.py`'s existing fallback contract for the
  pattern this repo already uses for exactly this situation.

TTLs are per-endpoint defaults reflecting how fast each thing actually
changes (rosters move on waiver day, league settings almost never do); every
default can be overridden per-call, and `None` always means "trust an
existing cache file forever" for data that cannot change once written (a
completed draft's pick log).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .cache import (
    DEFAULT_CACHE_DIR,
    UrlOpener,
    _default_opener,
    fetch_json,
    fetch_uncached_json,
)

_V1 = "https://api.sleeper.app/v1"
# Undocumented endpoints live on the api.sleeper.com host in every example
# seen during scoping; api.sleeper.app also answers them, but .com is what
# the (unofficial) community references use, so it's kept distinct here in
# case that ever stops being true of one host and not the other.
_UNDOCUMENTED_BASE = "https://api.sleeper.com"

# Every fantasy-relevant offense/kicker/defense position the season and
# weekly projection endpoints accept in one call.
PROJECTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


class SleeperClient:
    """Bound cache/opener/clock so call sites never see those details —
    same design as `ffbot.projections.resolve_provider` binding network
    details into a `ProjectionProvider` closure at config-resolution time.
    """

    def __init__(
        self,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        opener: UrlOpener = _default_opener,
        now: Optional[float] = None,
        force_refresh: bool = False,
    ) -> None:
        self.cache_dir = cache_dir
        self.opener = opener
        self.now = now
        # The GUI's "Refresh" button's mechanism: when set, every cached
        # endpoint this client touches is forced to refetch once, no matter
        # its TTL. "Once" (tracked per cache key in `_forced`, not a blanket
        # `force=True` on every call) is what preserves the "one HTTP
        # request per run" sharing that `report.load_everything` already
        # relies on -- e.g. `rosters()` is called by `fetch_my_roster`,
        # `waiver_position`, and the standings branch; a second call within
        # the same run must hit the now-fresh cache, not force a THIRD
        # fetch.
        self.force_refresh = force_refresh
        self._forced: set[str] = set()

    def _get(self, cache_key: str, url: str, ttl_minutes: float | None):
        force = False
        if self.force_refresh and cache_key not in self._forced:
            force = True
            self._forced.add(cache_key)
        return fetch_json(
            cache_key, url, cache_dir=self.cache_dir, ttl_minutes=ttl_minutes,
            opener=self.opener, now=self.now, force=force,
        )

    def _get_live(self, url: str):
        return fetch_uncached_json(url, opener=self.opener)

    # --- Documented ----------------------------------------------------

    def nfl_state(self, ttl_minutes: float = 60.0) -> dict:
        """Current week/season/season_type — replaces
        `ffbot.projections.current_nfl_season()`'s calendar-based guess with
        the real value Sleeper is using right now."""
        return self._get("state_nfl", f"{_V1}/state/nfl", ttl_minutes)

    def user(self, username: str, ttl_minutes: float = 1440.0) -> Optional[dict]:
        """`None` on a 404 (unknown username) rather than raising — an
        unknown username is a normal outcome of `scripts/whoami.py`'s
        discovery flow, not a fetch failure."""
        try:
            return self._get(f"user_{username}", f"{_V1}/user/{username}", ttl_minutes)
        except Exception:
            return None

    def user_leagues(self, user_id: str, season: int, ttl_minutes: float = 360.0) -> list[dict]:
        return self._get(
            f"user_leagues_{user_id}_{season}",
            f"{_V1}/user/{user_id}/leagues/nfl/{season}",
            ttl_minutes,
        )

    def league(self, league_id: str, ttl_minutes: float = 360.0) -> dict:
        """`roster_positions`, `scoring_settings`, `settings` (incl.
        `waiver_budget`), `draft_id`, `status`."""
        return self._get(f"league_{league_id}", f"{_V1}/league/{league_id}", ttl_minutes)

    def rosters(self, league_id: str, ttl_minutes: float = 30.0) -> list[dict]:
        """Every team's `players`/`starters`/`owner_id`/`roster_id`/
        `settings` (wins/losses/fpts/waiver_budget_used). Short TTL — this is
        the one endpoint most likely to change mid-week (waivers, trades)."""
        return self._get(f"rosters_{league_id}", f"{_V1}/league/{league_id}/rosters", ttl_minutes)

    def league_users(self, league_id: str, ttl_minutes: float = 360.0) -> list[dict]:
        """Team display names — `metadata.team_name`, joined to `rosters()`
        by `user_id`/`owner_id`."""
        return self._get(f"users_{league_id}", f"{_V1}/league/{league_id}/users", ttl_minutes)

    def matchups(self, league_id: str, week: int, ttl_minutes: float = 15.0) -> list[dict]:
        return self._get(
            f"matchups_{league_id}_wk{week:02d}",
            f"{_V1}/league/{league_id}/matchups/{week}",
            ttl_minutes,
        )

    def transactions(self, league_id: str, week: int, ttl_minutes: float = 30.0) -> list[dict]:
        """`<week>` is Sleeper's "round" — waivers/adds/drops/trades,
        including the winning FAAB bid in `settings.waiver_bid` on a
        `waiver`-type entry. Losing bids are not exposed by this API."""
        return self._get(
            f"transactions_{league_id}_wk{week:02d}",
            f"{_V1}/league/{league_id}/transactions/{week}",
            ttl_minutes,
        )

    def traded_picks(self, league_id: str, ttl_minutes: float = 360.0) -> list[dict]:
        return self._get(f"traded_picks_{league_id}", f"{_V1}/league/{league_id}/traded_picks", ttl_minutes)

    def league_drafts(self, league_id: str, ttl_minutes: float = 60.0) -> list[dict]:
        return self._get(f"league_drafts_{league_id}", f"{_V1}/league/{league_id}/drafts", ttl_minutes)

    def user_drafts(self, user_id: str, season: int, ttl_minutes: float = 60.0) -> list[dict]:
        return self._get(
            f"user_drafts_{user_id}_{season}", f"{_V1}/user/{user_id}/drafts/nfl/{season}", ttl_minutes,
        )

    def draft(self, draft_id: str) -> dict:
        """Always live, never cached — `status`/`last_picked` are exactly
        what a live-draft poll loop watches for change (see
        `ffbot.draft_sync`), and a stale read here is worse than one extra
        cheap GET."""
        return self._get_live(f"{_V1}/draft/{draft_id}")

    def draft_picks(self, draft_id: str) -> list[dict]:
        """Always live, never cached — the actual pick feed a live draft
        polls. Each pick carries `pick_no`, `player_id`, `roster_id`,
        `picked_by`, and self-describing `metadata` (name/position/team),
        so a poller never needs a separate players-dump call to render one."""
        return self._get_live(f"{_V1}/draft/{draft_id}/picks")

    def draft_traded_picks(self, draft_id: str) -> list[dict]:
        return self._get_live(f"{_V1}/draft/{draft_id}/traded_picks")

    def players(self, ttl_minutes: float = 1440.0) -> dict:
        """The full NFL players dump, `{player_id: {...}}`. ~5MB+; Sleeper's
        own docs ask callers not to poll this more than once a day, hence
        the long default TTL. Defense entries are keyed by team abbreviation
        (`"KC"`, `"SF"`, ...) with `team` populated — no separate defense-
        name-guessing workaround needed, unlike the FantasyPros board path
        (`names.defense_key`)."""
        return self._get("players_nfl", f"{_V1}/players/nfl", ttl_minutes)

    def trending(self, kind: str = "add", lookback_hours: int = 24, limit: int = 25, ttl_minutes: float = 60.0) -> list[dict]:
        """`kind` is `"add"` or `"drop"`. Returns `[{player_id, count}]` —
        `count` is the number of leagues with that add/drop in the window,
        not a percentage; join to `players()` for identity."""
        if kind not in ("add", "drop"):
            raise ValueError(f"trending kind must be 'add' or 'drop', got {kind!r}")
        url = f"{_V1}/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}"
        return self._get(f"trending_{kind}_{lookback_hours}h_{limit}", url, ttl_minutes)

    # --- Undocumented — always isolate behind a fallback at the call site ---

    def season_projections(
        self, season: int, positions=PROJECTION_POSITIONS, ttl_minutes: float = 360.0,
    ) -> list[dict]:
        """Undocumented. Season-total projections AND format-specific ADP
        (`adp_ppr`, `adp_std`, ...) in the SAME response — verified live
        during scoping (2026: 3300 rows, 635 with points, 306 with real PPR
        ADP). No bye-week field and no ADP standard-deviation field; callers
        needing either still need the board CSVs. Raises `SleeperFetchError`
        on failure — never call this without a fallback path, same
        discipline as `ffbot/projections/sleeper.py`'s weekly counterpart.
        """
        pos_qs = "&".join(f"position[]={p}" for p in positions)
        url = f"{_UNDOCUMENTED_BASE}/projections/nfl/{season}?season_type=regular&{pos_qs}&order_by=adp_ppr"
        return self._get(f"season_projections_{season}", url, ttl_minutes)

    def ownership(self, season: int, week: int, ttl_minutes: float = 360.0) -> dict[str, dict]:
        """Undocumented. `{player_id: {"owned": pct, "started": pct}}` —
        verified live during scoping (2026 wk1: 1692 entries). This is the
        real analog of Yahoo's `percent_owned`, which was never populated on
        any route this repo has ever run (see `ffbot.policy`'s three
        currently-inert guardrails). `started` has no Yahoo equivalent at
        all — a genuine new signal, not just a replacement. DEF entries are
        keyed by team abbreviation, same convention as `players()`.
        """
        url = f"{_UNDOCUMENTED_BASE}/players/nfl/research/regular/{season}/{week}"
        return self._get(f"ownership_{season}_wk{week:02d}", url, ttl_minutes)
