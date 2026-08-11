"""JSON serializers for the web GUI (`scripts/gui.py`).

Two entry points, mirroring the two text UIs they replace:

- `draft_state_json(ui_state)` mirrors every section of `draft_ui.render()`.
- `weekly_report_json(...)` mirrors `scripts/week_report.py`'s `main()` —
  same section gating (no board -> no roster status, etc.), same "commit
  the lineup or don't" choice, just returning plain dicts instead of
  printing text.

Every dataclass reachable from here (`Recommendation`, `WeekBrief`,
`WaiverCandidate`, ...) already exists; this module only flattens them into
JSON-safe dicts. No new computation happens here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from . import denial, policy
from . import roster_source as rs
from . import week
from .board import to_player
from .draft import alerts, demand_ahead, needs_between, picks_until, recommend, round_and_slot
from .draft_ui import UiState, _pool, _sorted_recs
from .lineup import optimize
from .names import normalize_name, search_scored
from .report import LoadedReport


def draft_state_json(state: UiState) -> dict:
    """Everything `draft_ui.render()` shows, as JSON-safe data."""
    draft = state.draft
    cfg = state.cfg
    current = draft.current_pick()
    round_, slot_on_clock = round_and_slot(current, draft.num_teams, draft.order)
    next_pick = draft.next_my_pick()
    upcoming = [p for p in draft.my_picks() if p >= current][:2]
    on_the_clock = slot_on_clock == draft.my_slot

    header = {
        "pick": current,
        "round": round_,
        "slot_on_clock": slot_on_clock,
        "on_the_clock": on_the_clock,
        "my_slot": draft.my_slot,
        "num_teams": draft.num_teams,
        "rounds": draft.rounds,
        "order": draft.order,
        "next_my_pick": next_pick,
        "upcoming": upcoming,
        "sync_status": state.sync_status,
        # How many picks stand between now and my next turn -- 0 exactly
        # when `on_the_clock` is true. Drives the GUI's planning-mode
        # banner; see `picks_until`.
        "picks_until_mine": picks_until(current, draft.my_picks()),
        "mode": "on_clock" if on_the_clock else "planning",
    }

    pending = [
        {
            "index": i,
            "key": bp.key,
            "name": bp.name,
            "position": bp.position,
            "team": bp.team,
            "proj": bp.points,
            "adp": bp.adp,
        }
        for i, bp in enumerate(state.pending, start=1)
    ]

    recommendations: list[dict] = []
    if not state.pending:
        recs = recommend(draft, cfg, limit=cfg.draft.gui_recommend_count, position=state.filter_pos)
        recs = _sorted_recs(recs, state.sort)
        for i, r in enumerate(recs, start=1):
            bp = r.player
            # `r.reason` is a single pre-joined "; "-separated string (see
            # `draft._reason`); split it back into parts so the GUI can
            # style the researched note distinctly from the structural
            # reasons, without changing `Recommendation`'s contract or the
            # TUI, which renders the joined string as-is.
            why_parts = [p for p in r.reason.split("; ") if p]
            intel_note = bp.intel_note or ""
            if intel_note and why_parts and why_parts[0] == intel_note:
                why_parts = why_parts[1:]
            recommendations.append(
                {
                    "rank": i,
                    "key": bp.key,
                    "name": bp.name,
                    "position": bp.position,
                    "team": bp.team,
                    "bye_week": bp.bye_week,
                    "proj": bp.points,
                    "vor": bp.vor,
                    "need": r.need,
                    "value": r.value,
                    "adp": bp.adp,
                    "survival": r.survival,
                    "upside": r.upside,
                    "arbitrage": r.arbitrage,
                    "scoring_edge": r.scoring_edge,
                    "why": r.reason,
                    "why_parts": why_parts,
                    "intel_note": intel_note,
                    "flags": list(r.flags),
                }
            )

    roster = [
        {"key": bp.key, "position": bp.position, "name": bp.name, "proj": bp.points}
        for bp in draft.my_roster()
    ]

    # Every known pick, newest first, with its drafting slot derived (see
    # `DraftState.slot_for`) -- replaces the old fixed 5-row "last picks"
    # list so the GUI can scroll the entire draft.
    draft_log = []
    for p in reversed([pk for pk in draft.picks if pk.key is not None]):
        bp = draft.board.by_key.get(p.key)
        draft_log.append(
            {
                "number": p.number,
                "mine": p.mine,
                "slot": draft.slot_for(p),
                "key": p.key,
                "name": bp.name if bp is not None else p.key,
            }
        )

    # Every team's roster so far, keyed by draft slot (see
    # `DraftState.rosters_by_slot`) -- backs the GUI's collapsed Opponents
    # panel. `unfilled` runs the same optimizer the lineup path uses, so it
    # is flex-aware rather than a naive per-position count.
    rosters_by_slot = draft.rosters_by_slot()
    opponents = []
    for slot in range(1, draft.num_teams + 1):
        slot_roster = rosters_by_slot.get(slot, [])
        roster_players = [to_player(bp, uid) for uid, bp in enumerate(slot_roster, start=1)]
        plan = optimize(roster_players, draft.roster_positions, None, cfg)
        opponents.append(
            {
                "slot": slot,
                "is_me": slot == draft.my_slot,
                "roster": [
                    {"key": bp.key, "position": bp.position, "name": bp.name, "proj": bp.points}
                    for bp in slot_roster
                ],
                "unfilled": sorted(set(plan.unfilled_slots)),
            }
        )

    return {
        "header": header,
        "pending": pending,
        "pending_mine": state.pending_mine,
        "recommendations": recommendations,
        "roster": roster,
        "alerts": alerts(draft, cfg),
        "needs_between": needs_between(draft),
        "draft_log": draft_log,
        "opponents": opponents,
        # Positional demand from teams picking before my next turn -- the
        # visible face of `block_weight` (see `draft.demand_ahead`), shown
        # regardless of whether that weight is actually on.
        "demand_ahead": demand_ahead(draft, cfg),
        "message": state.message,
        "sort": state.sort,
        "filter_pos": state.filter_pos,
        "should_quit": state.should_quit,
    }


def draft_search_json(state: UiState, query: str, limit: int = 8) -> dict:
    """Ranked name-search matches against the still-available pool, for the
    GUI's autocomplete dropdown.

    Reuses the exact same pool (`draft_ui._pool`) and scorer
    (`names.search_scored`) the enter-bar's own resolution uses, so a
    dropdown pick and a typed-and-submitted query can never disagree about
    who is still on the board.
    """
    query = query.strip()
    if not query:
        return {"matches": []}
    available = _pool(state.draft.board, state.draft.taken_keys())
    scored = search_scored(query, available)
    return {
        "matches": [
            {
                "key": bp.key,
                "name": bp.name,
                "position": bp.position,
                "team": bp.team,
                "adp": bp.adp,
                "proj": bp.points,
            }
            for _, bp in scored[:limit]
        ]
    }


def weekly_report_json(
    loaded: LoadedReport,
    week_num: int,
    lineup_state_path: str | Path = "weekly/lineup_state.yml",
    stream_positions: Sequence[str] | None = None,
    show_waivers: bool = False,
    remaining_faab: int | None = None,
    my_priority: int | None = None,
    weeks_in_season: int = 17,
    commit_lineup: bool = False,
) -> dict:
    """Everything `scripts/week_report.py`'s `main()` prints, as JSON-safe
    data. `commit_lineup=False` (the default) is a what-if run: the lineup
    is computed against the last *committed* state but nothing is written,
    mirroring the CLI's `--no-save-state`. The GUI should pass
    `commit_lineup=True` only from an explicit "commit this lineup" action.
    """
    cfg, weekly, board = loaded.cfg, loaded.weekly, loaded.board
    players, unmatched = loaded.players, loaded.unmatched
    stadiums, league_rosters = loaded.stadiums, loaded.league_rosters

    lineup_state = rs.load_lineup_state(lineup_state_path)
    players = rs.apply_lineup_state(players, lineup_state)

    brief = week.build_week_brief(
        players, cfg.roster_positions, week_num, cfg, weekly, stadiums,
        board=board, league_rosters=league_rosters,
    )
    if commit_lineup:
        rs.save_lineup_state(lineup_state_path, brief.lineup)

    result: dict = {
        "week": brief.week,
        "alerts": list(brief.alerts),
        "unmatched_warnings": list(brief.unmatched_warnings),
        "lineup": {
            "is_noop": brief.lineup.is_noop(),
            "moves": [str(m) for m in brief.lineup.moves],
            "unfilled_slots": list(brief.lineup.unfilled_slots),
            "assignments": [
                {"slot": slot, "name": p.name, "proj": p.projected_points, "player_id": p.player_id}
                for slot, p in sorted(brief.lineup.assignments, key=lambda t: t[0])
            ],
            "bench": [{"name": p.name, "proj": p.projected_points} for p in brief.lineup.bench],
        },
        "notes": [{"name": n.name, "note": n.note, "flags": list(n.flags)} for n in brief.notes],
        "unmatched_roster": [{"query": m.query, "suggestion": m.suggestion} for m in unmatched],
        "league_rosters_loaded": len(league_rosters.teams),
        "committed": commit_lineup,
    }

    if board is not None:
        status = week.build_roster_status(players, cfg.roster_positions, board, cfg)
        result["roster_status"] = {
            "occupied": status.space.occupied,
            "capacity": status.space.capacity,
            "open_spots": status.space.open_spots,
            "ir_parked": status.space.ir_parked,
            "missing": list(status.missing),
            "core": [
                {"name": c.name, "hold_margin": c.hold_margin}
                for c in sorted(status.core, key=lambda c: -c.hold_margin)
            ],
            "stream": [
                {"name": c.name, "hold_margin": c.hold_margin}
                for c in sorted(status.stream, key=lambda c: -c.hold_margin)
            ],
        }

    if stream_positions and board is not None:
        rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
        pool = [bp for bp in board.players if normalize_name(bp.name) not in rostered_names]
        streamers: dict[str, list[dict]] = {}
        for pos in stream_positions:
            pos_u = pos.upper()
            candidates = week.rank_streamers(pool, pos_u, weekly, cfg.season, week=week_num, stadiums=stadiums)
            streamers[pos_u] = [
                {"name": c.name, "team": c.team, "value": c.weekly_value, "reason": c.reason}
                for c in candidates
            ]
        result["streamers"] = streamers

    if show_waivers and board is not None:
        waiver_type = cfg.league.waiver_type if cfg.league is not None else "faab"
        weeks_remaining = max(1, weeks_in_season - week_num + 1)
        candidates, missing = week.waiver_candidates(
            players,
            board,
            cfg.roster_positions,
            cfg,
            remaining_faab=remaining_faab or 0,
            my_priority=my_priority,
            weeks_remaining=weeks_remaining,
            league_rosters=league_rosters,
            week=week_num,
        )
        result["waivers"] = {
            "waiver_type": waiver_type,
            "missing": list(missing),
            "candidates": [
                {
                    "add_name": c.add_name,
                    "position": c.position,
                    "value": c.value,
                    "net": c.net,
                    "drop_name": c.drop_name,
                    "drop_reason": c.drop_reason,
                    "max_bid": c.max_bid,
                    "claim_note": c.claim_note,
                    "reason": c.reason,
                }
                for c in candidates
            ],
        }

        ir_candidates = week.ir_stash_candidates(
            players, board, cfg.roster_positions, weekly, cfg, league_rosters=league_rosters
        )
        result["ir_stash"] = [
            {"add_name": c.add_name, "position": c.position, "value": c.value, "reason": c.reason}
            for c in ir_candidates
        ]

        if cfg.season.denial_weight != 0.0 and league_rosters.teams:
            roster_keys, _ = week.roster_board_keys(players, board)
            rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
            streaming_floor = week.best_streaming_baseline(roster_keys, board, cfg)
            denial_list = denial.denial_candidates(
                roster_keys, board, cfg.roster_positions, cfg, league_rosters, rostered_names, streaming_floor,
            )
            if denial_list and waiver_type == "rolling":
                verdict = policy.can_deny_claim(my_priority or cfg.draft.num_teams, cfg)
                if not verdict.allowed:
                    result["denial_suppressed_reason"] = verdict.reason
                    denial_list = []
            result["denial_holds"] = [
                {
                    "add_name": c.add_name,
                    "position": c.position,
                    "denial_value": c.denial_value,
                    "reason": c.reason,
                }
                for c in denial_list
            ]

    return result
