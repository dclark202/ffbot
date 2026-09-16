"""League demand: what OTHER managers are doing about an unrostered player.

Every other in-season signal in this repo is a statement about a player --
what he is projected to score, how his role is trending, what the weather
will do to him. This module is the one statement about the *market*: how
many managers want him, measured rather than inferred.

That distinction is why this is not folded into `ffbot.denial`.
`denial.denial_value` already means "a rival needs him too" and answers it by
re-running the optimizer against each rival's roster -- a model of what they
*should* want, from rosters and curated standings. It reads no transaction
log, no ownership and no trending, and it has never seen a manager actually
do anything. What this module carries is the opposite kind of evidence:
observed behaviour, with no model in it at all. Merging the two would let an
inference launder itself as an observation, and would make the combined
number ungradeable both ways -- see W6 in docs/dev/INSEASON-FINDINGS.md for
why folding this into `denial_value` is deliberately NOT done.

DESCRIPTIVE ONLY, and structurally so. Nothing here reaches `net`, `value`,
`gain`, `urgency` or `denial_value`; it is attached to
`week.SpeculativeCandidate.demand` and `gameplan.PlayerMetrics.demand` and
rendered, exactly as `season_ptd` is. `tests/test_gameplan.py::
TestWaiverDemandIsDescriptiveOnly` proves it by building the same plan twice
with and without a wildly-perturbed signal and diffing every recommendation.
There is a second, harder reason beyond the usual "no backtest has graded
it": Sleeper's trending and ownership endpoints publish only a CURRENT
reading and keep no archive, so no replay of a past season can ever
reconstruct what the wire looked like at decision time. A valuation version
of this signal could only ever be graded FORWARD (BACKTEST.md's B16 item 2).

THE TIMING SPLIT, which is the whole reason `is_retrospective` exists.
Sleeper does not expose a claim until it has been processed. A rival's failed
claim -- the sharpest signal here, and the one that prompted this module --
does not exist until the weekly waiver run has already happened, so it can
never inform the Tuesday claims check that precedes that run. It is a
WEDNESDAY retrospective: a record of what Tuesday's advice missed. Only
`trending` and the ownership delta are genuinely available pre-run. A
consumer decides what it may say by reading `DemandSignal.is_retrospective`,
never by matching on `source` -- matching note prefixes is how a HOLD-PRIORITY
row got seated as "Add & start" on 2026-09-13.

Whether the endpoint returns PENDING claims at all is unverified (W7). Rather
than assume, `derive` treats a pending row as a live league-specific signal
if one ever appears and notes it once, the same way `availability` learns the
real weekly run time from the transaction log instead of trusting
`weekly_run_time_et` forever.

Pure: no network and no clock. `report.load_everything` does the fetching and
hands the raw payloads in, exactly as it does for `ffbot.availability`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .names import normalize_name


@dataclass(frozen=True)
class DemandSignal:
    """One piece of evidence that other managers want this player.

    `value`/`unit` are the number and its scale, kept separate so nothing
    downstream has to parse `text` to recover them -- the same typed-fields
    rule `gameplan.DecisionMetrics` follows. `text` is for display only.

    `is_retrospective` is True when the signal could not have been known at
    the moment the decision it accompanies was made (a processed waiver
    claim). It is a property of the SIGNAL, not of the run, so a consumer
    that must not claim foreknowledge filters on this field rather than on
    `source`.
    """

    source: str          # "sleeper_trending_add" | "ownership_delta" | "rival_failed_claims"
    value: float
    unit: str            # "leagues" | "pct_owned" | "claims"
    window: str          # "48h" | "wk01 -> wk02" | "waiver run 2026-09-16"
    as_of: str           # ISO-8601, or "" when the source carries no timestamp
    text: str            # rendered one-liner, display only
    is_retrospective: bool = False


# Relative weight of each source when ordering speculative rows. NOT a
# valuation: nothing here is in points, and none of it reaches `net`. It only
# decides which two of several flagged players are shown first, which is why
# it is a module constant rather than a config dial -- there is no decision
# for a human to tune here, and adding a slider would imply otherwise.
#
# A league-specific signal outranks a global one because twelve managers who
# share your waiver wire are better evidence about your wire than a million
# who do not.
_SOURCE_RANK = {
    "rival_failed_claims": 3.0,
    "ownership_delta": 2.0,
    "sleeper_trending_add": 1.0,
}


@dataclass
class WaiverDemand:
    """Every demand signal this run found, keyed by board key.

    `notes` carries anything learned about the SOURCE rather than about a
    player -- currently only the one-time pending-claim discovery (W7).
    """

    by_key: dict[str, tuple[DemandSignal, ...]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def signals_for(self, name: str, position: str) -> tuple[DemandSignal, ...]:
        return self.by_key.get(_key(name, position), ())

    def strength(self, name: str, position: str) -> float:
        """A unitless ordering key for the speculative section, and nothing
        else. Deliberately not exposed on any recommendation row: it exists
        to sort two flagged players, and calling it a score would invite
        exactly the comparison against `net` this module refuses to support.
        """
        return sum(_SOURCE_RANK.get(s.source, 0.0) for s in self.signals_for(name, position))


def _key(name: str, position: str) -> str:
    return f"{normalize_name(name)}:{position.upper()}"


def _player_identity(players: dict, player_id: str) -> tuple[str, str]:
    """`(name, position)` for a Sleeper player id, or `("", "")`.

    A team defense is stored under its team abbreviation rather than a
    person's name, same as everywhere else this repo touches the players
    dump -- `models`/`sleeper_roster` already carry that quirk.
    """
    info = players.get(player_id) or {}
    position = (info.get("position") or "").upper()
    name = info.get("full_name") or " ".join(
        x for x in (info.get("first_name") or "", info.get("last_name") or "") if x
    ).strip()
    if position == "DEF" and not name:
        name = info.get("team") or player_id
    return name, position


def trending_signals(
    rows: list[dict], players: dict, lookback_hours: int, as_of: str = ""
) -> dict[str, tuple[DemandSignal, ...]]:
    """Sleeper's global "adds in the last N hours" feed -> demand signals.

    `count` is a number of LEAGUES across all of Sleeper, not a percentage
    (see `SleeperClient.trending`). Pre-run and available in real time, which
    makes this the only genuinely Tuesday-usable signal here -- and the
    weakest, because it says nothing about the twelve managers who actually
    share your wire.
    """
    out: dict[str, tuple[DemandSignal, ...]] = {}
    for row in rows or []:
        pid = str(row.get("player_id") or "")
        count = row.get("count")
        if not pid or not isinstance(count, (int, float)):
            continue
        name, position = _player_identity(players, pid)
        if not name or not position:
            continue
        out[_key(name, position)] = (
            DemandSignal(
                source="sleeper_trending_add",
                value=float(count),
                unit="leagues",
                window=f"{lookback_hours}h",
                as_of=as_of,
                text=f"{int(count):,} leagues added him in {lookback_hours}h",
            ),
        )
    return out


def ownership_delta_signals(
    current: dict, prior: dict, players: dict, week: int, min_delta: float = 1.0,
) -> dict[str, tuple[DemandSignal, ...]]:
    """Week-over-week change in the share of Sleeper leagues rostering a
    player.

    Pre-run and real, but coarse and lagged -- the rookie that prompted this
    module moved 34.1% -> 36.5% across the week he was claimed in three
    leagues at once. `min_delta` exists so an ownership number that merely
    jittered does not read as demand; below it there is no entry at all,
    the same "absent, not zero" contract every other optional input here has.
    """
    out: dict[str, tuple[DemandSignal, ...]] = {}
    for pid, cur in (current or {}).items():
        was = (prior or {}).get(pid)
        if not isinstance(cur, dict) or not isinstance(was, dict):
            continue
        now_pct, then_pct = cur.get("owned"), was.get("owned")
        if not isinstance(now_pct, (int, float)) or not isinstance(then_pct, (int, float)):
            continue
        delta = float(now_pct) - float(then_pct)
        if delta < min_delta:
            continue
        name, position = _player_identity(players, str(pid))
        if not name or not position:
            continue
        out[_key(name, position)] = (
            DemandSignal(
                source="ownership_delta",
                value=delta,
                unit="pct_owned",
                window=f"wk{max(1, week - 1):02d} -> wk{week:02d}",
                as_of="",
                text=f"rostered in {then_pct:.1f}% -> {now_pct:.1f}% of leagues",
            ),
        )
    return out


def rival_claim_signals(
    transactions: list[dict], players: dict, my_roster_id: int | None = None,
) -> tuple[dict[str, tuple[DemandSignal, ...]], list[str]]:
    """How many managers in YOUR league claimed each player at the last run.

    This is the signal the 2026-09-16 finding is about, and the sharpest one
    here -- twelve managers who share your wire, with their money where their
    mouth is. `ffbot.availability` already fetches these transactions and
    then discards exactly this: `derive` keeps only `status == "complete"`,
    and `claim_outcomes` keeps only the user's own `roster_id`. A rival's
    FAILED claim is a manager who wanted a player badly enough to spend a
    waiver slot on him and lost, which is the cleanest possible statement of
    contestedness, and it was being thrown away every run.

    RETROSPECTIVE, and the returned signals say so. A claim does not appear
    in the log until Sleeper has processed it, so this can never inform the
    Tuesday check that precedes the run -- only Wednesday's, and the record.

    A `pending` row, if Sleeper ever returns one, is a live signal instead
    (W7); it is counted separately and reported once in the returned notes
    rather than assumed either way.
    """
    contested: dict[str, set] = {}
    pending: dict[str, set] = {}
    run_at = ""
    for t in transactions or []:
        if t.get("type") != "waiver":
            continue
        status = t.get("status")
        if status not in ("complete", "failed", "pending"):
            continue
        for pid in (t.get("adds") or {}):
            name, position = _player_identity(players, str(pid))
            if not name or not position:
                continue
            bucket = pending if status == "pending" else contested
            for rid in (t.get("roster_ids") or []):
                if my_roster_id is not None and rid == my_roster_id:
                    continue
                bucket.setdefault(_key(name, position), set()).add(rid)
        stamp = t.get("status_updated") or t.get("created")
        if status == "complete" and isinstance(stamp, (int, float)):
            run_at = max(run_at, _iso(stamp))

    out: dict[str, tuple[DemandSignal, ...]] = {}
    for key, rosters in contested.items():
        if not rosters:
            continue
        out[key] = (
            DemandSignal(
                source="rival_failed_claims",
                value=float(len(rosters)),
                unit="claims",
                window=f"waiver run {run_at[:10]}" if run_at else "last waiver run",
                as_of=run_at,
                text=(
                    f"{len(rosters)} rival{'s' if len(rosters) != 1 else ''} "
                    "claimed him at the last run"
                ),
                is_retrospective=True,
            ),
        )

    notes: list[str] = []
    for key, rosters in pending.items():
        out[key] = out.get(key, ()) + (
            DemandSignal(
                source="rival_failed_claims",
                value=float(len(rosters)),
                unit="claims",
                window="pending, this cycle",
                as_of="",
                text=f"{len(rosters)} rival(s) have a claim in for him right now",
            ),
        )
    if pending:
        notes.append(
            f"Waiver demand -- Sleeper returned {len(pending)} pending claim(s) this run; "
            "pending claims are a live pre-run signal after all (see W7 in "
            "docs/dev/INSEASON-FINDINGS.md)."
        )
    return out, notes


def _iso(epoch_ms: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat()


def derive(*sources: dict[str, tuple[DemandSignal, ...]], notes: list[str] | None = None) -> WaiverDemand:
    """Merge per-source maps into one `WaiverDemand`.

    Signals ACCUMULATE per player rather than overwriting: three managers
    claiming him and forty thousand leagues adding him are two facts, not a
    disagreement about one. Every source is independently optional, so a
    failed `trending` fetch costs the ownership delta nothing.
    """
    by_key: dict[str, tuple[DemandSignal, ...]] = {}
    for source in sources:
        for key, signals in (source or {}).items():
            by_key[key] = by_key.get(key, ()) + tuple(signals)
    return WaiverDemand(by_key=by_key, notes=list(notes or []))
