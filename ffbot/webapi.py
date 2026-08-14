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
from .names import defense_key, normalize_name, search_scored
from .report import LoadedReport
from .weekly_editor import editor_json_from_intel


def _bp_team(bp) -> str:
    """A `BoardPlayer`'s team abbreviation, resolved for defenses.

    `draft/proj_dst.csv` (and FantasyPros exports generally) routinely ship
    a blank or full-city-name `team` field for DEF rows -- the same gap
    `week._resolve_team` papers over for the weekly path via this exact
    helper (`names.defense_key`). Without it every defense on the roster
    and Opponents panels reads "-" instead of its real abbreviation.
    """
    if bp.position != "DEF":
        return bp.team
    return defense_key(bp.name, bp.team) or bp.team


def _player_team(p) -> str:
    """A roster `Player`'s team abbreviation, resolved for defenses -- the
    `Player` analog of `_bp_team` above, reusing `week._resolve_team`'s
    exact defense-key resolution so a defense's matchup lookup agrees
    across every panel."""
    pos = p.eligible_positions[0] if p.eligible_positions else ""
    return week._resolve_team(pos, p.team, p.name)


def _matchup_row(p, weekly: week.WeeklyIntel) -> dict:
    """`{opponent, kickoff_et, home}` for a roster player's game this week,
    all `None` when no researched/live `GameInfo` exists for their team --
    the "My team" panel's opponent/kickoff columns read straight off this."""
    game = weekly.games.get(_player_team(p))
    if game is None:
        return {"opponent": None, "kickoff_et": None, "home": None}
    return {"opponent": game.opponent, "kickoff_et": game.kickoff_et or None, "home": game.home}


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
        # The pick every `survival` number in `recommendations` is measured
        # to -- my next turn STRICTLY after this one, so it stays the
        # meaningful "if I pass, is he still there?" pick even while I am on
        # the clock (where `next_my_pick` is the current pick itself). The
        # GUI labels the Surv column with it. None = no picks of mine left.
        "survival_to_pick": draft.next_my_pick_after(current),
        "upcoming": upcoming,
        "sync_status": state.sync_status,
        # Why sync_status is "off" -- e.g. "draft/sleeper_ids.json not
        # found". "" whenever there's nothing to explain (sync live/
        # degraded, or --sync/--no-sync was never attempted this session).
        "sync_reason": state.sync_reason,
        "sync_unmapped": state.sync_unmapped,
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
        recs = recommend(
            draft, cfg, limit=cfg.draft.gui_recommend_count, position=state.filter_pos,
            kalshi_scores=state.kalshi_scores,
        )
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
        {
            "key": bp.key,
            "position": bp.position,
            "name": bp.name,
            "team": _bp_team(bp),
            "bye_week": bp.bye_week,
            "proj": bp.points,
        }
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
                "round": round_and_slot(p.number, draft.num_teams, draft.order)[0],
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
                    {
                        "key": bp.key,
                        "position": bp.position,
                        "name": bp.name,
                        "team": _bp_team(bp),
                        "bye_week": bp.bye_week,
                        "proj": bp.points,
                    }
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
    GUI's autocomplete dropdown and enter-bar.

    Reuses the exact same pool (`draft_ui._pool`) and scorer
    (`names.search_scored`) the enter-bar's own resolution uses, so a
    dropdown pick and a typed-and-submitted query can never disagree about
    who is still on the board.

    `unambiguous_key` is the SAME single-match-or-unique-exact-match rule
    `draft_ui._search_and_pick` uses to auto-pick without showing a menu —
    computed against the full ranked list, before the `limit` truncation
    below, so a genuinely unique exact match is never missed just because
    other loose matches happened to fill the display slice ahead of the
    exact one in a pathological ranking. `None` means "ambiguous — show the
    dropdown for an explicit choice," exactly mirroring the pending-menu
    case in the shared command grammar.
    """
    query = query.strip()
    if not query:
        return {"matches": [], "unambiguous_key": None}
    available = _pool(state.draft.board, state.draft.taken_keys())
    scored = search_scored(query, available)
    unambiguous_key = None
    if scored:
        top_score, top = scored[0]
        exact_and_unique = top_score == 100 and (len(scored) == 1 or scored[1][0] < 100)
        if len(scored) == 1 or exact_and_unique:
            unambiguous_key = top.key
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
        ],
        "unambiguous_key": unambiguous_key,
    }


def weekly_report_json(
    loaded: LoadedReport,
    week_num: int,
    lineup_state_path: str | Path = "weekly/lineup_state.yml",
    stream_positions: Sequence[str] | None = None,
    show_waivers: bool = False,
    my_priority: int | None = None,
    weeks_in_season: int = 17,
    commit_lineup: bool = False,
    week_source: str = "explicit",
    refreshed: bool = False,
) -> dict:
    """Everything the GUI's read-only weekly page renders, as JSON-safe
    data -- the live-Sleeper analog of what `scripts/week_report.py`'s
    `main()` prints for the file route.

    `loaded.slots_source == "sleeper"` means `players[...].selected_position`
    already reflects the REAL current lineup in the Sleeper app (see
    `report.load_everything`/`sleeper_roster.starters_slot_map`) -- in that
    case `weekly/lineup_state.yml` is skipped entirely, both as the
    baseline (live starters already ARE the baseline) and as a write target
    on `commit_lineup=True` (there is nothing meaningful to commit; a move
    means "change this in the Sleeper app now"). Under the file route
    (`slots_source == "file"`), behavior is unchanged from before this
    parameter existed: `commit_lineup=False` (the default) is a what-if
    run against the last *committed* state; `commit_lineup=True` writes it.

    `week_source`/`refreshed` are pass-through echoes of how the caller
    resolved `week_num` and whether this run bypassed Sleeper's normal
    caches (see `scripts/gui.py`'s `weekly_run_action`) -- purely for the
    page's header badges, no effect on any computation here.
    """
    cfg, weekly, board = loaded.cfg, loaded.weekly, loaded.board
    players, unmatched = loaded.players, loaded.unmatched
    stadiums, league_rosters = loaded.stadiums, loaded.league_rosters

    live_slots = loaded.slots_source == "sleeper"
    if not live_slots:
        lineup_state = rs.load_lineup_state(lineup_state_path)
        players = rs.apply_lineup_state(players, lineup_state)

    # Explicit my_priority always wins; otherwise fall back to the live
    # Sleeper value (roster_source: sleeper only -- see report.load_everything)
    # so an unset priority no longer silently assumes the cheapest, least-
    # urgent case. Resolved once, up front, so both the waiver-candidate
    # scan and the denial-hold guardrail below read the same number.
    resolved_priority = my_priority if my_priority is not None else loaded.waiver_priority

    brief = week.build_week_brief(
        players, cfg.roster_positions, week_num, cfg, weekly, stadiums,
        board=board, league_rosters=league_rosters,
    )
    if commit_lineup and not live_slots:
        rs.save_lineup_state(lineup_state_path, brief.lineup)

    cause = {p.player_id: r for p, r in brief.lineup.benched_for_cause}

    result: dict = {
        "week": brief.week,
        "week_source": week_source,
        "refreshed": refreshed,
        # Live-projection/roster/league-rosters alerts (e.g. a Sleeper fetch
        # failure) come first -- a data-source problem is more urgent than
        # an ordinary roster note.
        "alerts": (
            list(loaded.projection_alerts)
            + list(loaded.roster_source_alerts)
            + list(loaded.league_rosters_alerts)
            + list(loaded.game_conditions_alerts)
            + list(loaded.standings_alerts)
            + list(brief.alerts)
        ),
        "projection_source": loaded.projection_source,
        "roster_source": loaded.roster_source,
        "slots_source": loaded.slots_source,
        "unmatched_warnings": list(brief.unmatched_warnings),
        "lineup": {
            "is_noop": brief.lineup.is_noop(),
            "moves": [str(m) for m in brief.lineup.moves],
            "unfilled_slots": list(brief.lineup.unfilled_slots),
            "assignments": [
                {
                    "slot": slot, "name": p.name,
                    "position": p.eligible_positions[0] if p.eligible_positions else "",
                    "team": _player_team(p),
                    "proj": p.projected_points, "player_id": p.player_id,
                    "status": p.status,
                    **_matchup_row(p, weekly),
                }
                for slot, p in sorted(brief.lineup.assignments, key=lambda t: t[0])
            ],
            "bench": [
                {
                    "name": p.name,
                    "position": p.eligible_positions[0] if p.eligible_positions else "",
                    "team": _player_team(p),
                    "proj": p.projected_points, "player_id": p.player_id,
                    "status": p.status,
                    "reason": cause.get(p.player_id),  # None = plain outscored, not bye/OUT
                    **_matchup_row(p, weekly),
                }
                for p in brief.lineup.bench
            ],
            "ir": [
                {
                    "name": p.name,
                    "position": p.eligible_positions[0] if p.eligible_positions else "",
                    "team": _player_team(p),
                    "proj": p.projected_points, "player_id": p.player_id,
                    "status": p.status,
                    "reason": p.selected_position,  # the actual IR/IR-R/TAXI slot label
                    **_matchup_row(p, weekly),
                }
                for p in brief.lineup.held_in_ir
            ],
        },
        "notes": [{"name": n.name, "note": n.note, "flags": list(n.flags)} for n in brief.notes],
        "unmatched_roster": [{"query": m.query, "suggestion": m.suggestion} for m in unmatched],
        "league_rosters": {
            "source": loaded.league_rosters_source,
            "teams_count": len(league_rosters.teams),
            "fetched_live": loaded.league_rosters_source == "sleeper",
            "generated": league_rosters.generated,
            "unmatched_count": len(league_rosters.unmatched),
        },
        "committed": commit_lineup and not live_slots,
        # The priority actually used this run, and whether it came from you
        # or a live Sleeper fetch -- lets the GUI prefill the priority field
        # instead of showing a blank box that silently means "no urgency".
        "waiver_priority": resolved_priority,
        "waiver_priority_source": "explicit" if my_priority is not None else ("sleeper" if loaded.waiver_priority is not None else None),
    }

    # A researched-notes/matchups view for the roster's own players --
    # read-only, matchup-centric (reuses the GUI's former editor transform
    # for the matchups half so both surfaces stay in exact agreement).
    intel_editor = editor_json_from_intel(weekly)
    intel_players: list[dict] = []
    seen_intel_names: set[str] = set()
    for p in players:
        key = normalize_name(p.name)
        if key in seen_intel_names:
            continue
        seen_intel_names.add(key)
        entry = weekly.players.get(key)
        if entry is None:
            continue
        intel_players.append({
            "name": p.name, "status": entry.status, "note": entry.note,
            "flags": list(entry.flags), "risk": entry.risk, "upside": entry.upside,
        })
    result["intel"] = {
        "week": weekly.week,
        "generated": weekly.generated,
        "source_notes": weekly.source_notes,
        "players": intel_players,
        "matchups": intel_editor["matchups"],
    }

    # loaded.ros_board (real rest-of-season points, when a live provider
    # supplied them -- see report.load_everything) feeds every function
    # below that values a player at season scale (rank_streamers,
    # waiver_candidates' ros_gain/drop_cost); `board` (the frozen season
    # board) is the fallback, same as before this existed.
    valuation_pool = loaded.ros_board or board

    if stream_positions and board is not None:
        rostered_names = {normalize_name(p.name) for p in players} | league_rosters.rostered_names()
        pool = [bp for bp in valuation_pool.players if normalize_name(bp.name) not in rostered_names]
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
        candidates, missing = week.waiver_candidates(
            players,
            valuation_pool,
            cfg.roster_positions,
            cfg,
            my_priority=resolved_priority,
            # A FIXED season length, matching `season_board_rows`'s own
            # fallback-pricing convention (see report.load_everything) --
            # not a shrinking "weeks remaining", which would desync a
            # candidate's board-fallback price from a rostered player's.
            weeks_remaining=weeks_in_season,
            league_rosters=league_rosters,
            week=week_num,
            weekly=weekly,
            weekly_points=loaded.weekly_points or None,
        )
        result["waivers"] = {
            "missing": list(missing),
            "candidates": [
                {
                    "add_name": c.add_name,
                    "position": c.position,
                    "value": c.value,
                    "net": c.net,
                    "drop_name": c.drop_name,
                    "drop_reason": c.drop_reason,
                    "claim_note": c.claim_note,
                    "reason": c.reason,
                }
                for c in candidates
            ],
        }

        ir_candidates = week.ir_stash_candidates(
            players, valuation_pool, cfg.roster_positions, weekly, cfg, league_rosters=league_rosters
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
            if denial_list:
                verdict = policy.can_deny_claim(resolved_priority or cfg.draft.num_teams, cfg)
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
