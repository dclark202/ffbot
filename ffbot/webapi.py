"""JSON serializers for the web GUI (`scripts/gui.py`).

Two entry points, mirroring the two text UIs they replace:

- `draft_state_json(ui_state)` mirrors every section of `draft_ui.render()`.
- `weekly_report_json(...)` mirrors `scripts/week_report.py`'s `main()` —
  same section gating (no board -> no roster status, etc.), same "commit
  the lineup or don't" choice, just returning plain dicts instead of
  printing text. Its Recommendations payload (the `moves` key) is a thin
  JSON flattening of `ffbot.gameplan.build_gameplan` -- ALL the actual
  computation (one valuation pool, streaming/denial folded into ordinary
  add/drop rows, the coherent post-pickup base lineup, per-claim "if it
  clears" consequences) lives there, not here.

Every dataclass reachable from here (`Recommendation`, `WeekBrief`,
`GamePlan`, ...) already exists; this module only flattens them into
JSON-safe dicts. No new computation happens here.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from . import gameplan
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


def rec_row(r, rank: int) -> dict:
    """One `draft.Recommendation` as the GUI's recommendation-table row.

    Shared with `ffbot.draft_report` on purpose: the draft report exists to
    capture exactly what the table showed at each pick, so a second
    hand-maintained copy of these keys would drift and quietly make every
    stored report describe a table that never existed.
    """
    bp = r.player
    # `r.reason` is a single pre-joined "; "-separated string (see
    # `draft._reason`); split it back into parts so the GUI can style the
    # researched note distinctly from the structural reasons, without
    # changing `Recommendation`'s contract or the TUI, which renders the
    # joined string as-is.
    why_parts = [p for p in r.reason.split("; ") if p]
    intel_note = bp.intel_note or ""
    if intel_note and why_parts and why_parts[0] == intel_note:
        why_parts = why_parts[1:]
    return {
        "rank": rank,
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
        "volatility": r.volatility,
        "arbitrage": r.arbitrage,
        "scoring_edge": r.scoring_edge,
        # What this position is expected to still offer at my next pick --
        # the number `value` is discounted by. 0.0 whenever
        # `draft.scarcity_weight` is off, so the key is always present and
        # always meaningful.
        "later": r.later,
        "tier": bp.tier,
        "why": r.reason,
        "why_parts": why_parts,
        "intel_note": intel_note,
        "flags": list(r.flags),
    }


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
        # Footnote for a sync that IS live/degraded -- e.g. rehearsing
        # against a Sleeper mock draft. "" in the normal in-league case.
        "sync_note": state.sync_note,
        "sync_unmapped": state.sync_unmapped,
        # How often the page itself should poll for fresh state (drains any
        # sync picks queued since the last request) -- see
        # `DraftConfig.gui_poll_seconds`'s docstring for why this is a
        # separate dial from sync_poll_seconds (server-to-Sleeper).
        "poll_seconds": cfg.draft.gui_poll_seconds,
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
        recommendations = [rec_row(r, i) for i, r in enumerate(recs, start=1)]

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


def _swap_line_json(line: "gameplan.SwapLine") -> dict:
    return {
        "kind": line.kind, "slot": line.slot, "slot_display": line.slot_display,
        "from_slot": line.from_slot, "from_slot_display": line.from_slot_display,
        "start_name": line.start_name, "start_team": line.start_team, "start_pos": line.start_pos,
        "start_proj": line.start_proj,
        "bench_name": line.bench_name, "bench_team": line.bench_team, "bench_proj": line.bench_proj,
        "reason": line.reason, "opp_stack_note": line.opp_stack_note, "text": line.text,
    }


def _claim_consequence_json(c: "gameplan.ClaimConsequence | None") -> dict | None:
    if c is None:
        return None
    return {
        "starts": c.starts, "slot_display": c.slot_display, "over_name": c.over_name,
        "week_delta": c.week_delta, "text": c.text,
    }


def _adddrop_json(row: "gameplan.AddDropRec") -> dict:
    return {
        "kind": row.kind, "position": row.position,
        "add_name": row.add_name, "add_team": row.add_team,
        "drop_name": row.drop_name, "drop_team": row.drop_team, "drop_reason": row.drop_reason,
        "net": row.net, "value": row.value, "claim_note": row.claim_note,
        "reasons": list(row.reasons), "forced_need": row.forced_need,
        "denial_team": row.denial_team, "denial_gain": row.denial_gain, "on_bye": row.on_bye,
        "if_clears": _claim_consequence_json(row.if_clears),
        "text": row.text,
    }


def _opponent_json(loaded: LoadedReport, plan: "gameplan.GamePlan") -> dict:
    """The Recommendations panel's opponent strip -- who you're playing
    this week and their actual started lineup, so a discount/leverage
    reason in a `moves` row is never just an unverifiable claim."""
    name = loaded.cfg.league.my_opponent if loaded.cfg.league is not None else ""
    starters = [
        {"name": s.name, "team": s.team, "position": s.position} for s in loaded.opponent_starters
    ]
    their_total = None
    if loaded.opponent_starters and loaded.weekly_points:
        total, found_any = 0.0, False
        for s in loaded.opponent_starters:
            pts = loaded.weekly_points.get(f"{normalize_name(s.name)}:{s.position}")
            if pts is not None:
                total += pts
                found_any = True
        their_total = total if found_any else None
    my_total = sum(p.projected_points or 0.0 for _, p in plan.base_plan.assignments)
    return {"name": name, "starters": starters, "their_week_proj": their_total, "my_week_proj": my_total}


def weekly_report_json(
    loaded: LoadedReport,
    week_num: int,
    lineup_state_path: str | Path = "weekly/lineup_state.yml",
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

    `show_waivers` (the GUI always passes `True` -- there's no checkbox on
    a read-only page) gates the `moves`/`opponent` keys' PRESENCE, mirroring
    the pre-existing "waivers" key's on/off contract; `moves.start_sit`
    itself is computed by `ffbot.gameplan.build_gameplan` regardless, since
    a lineup recommendation isn't a waiver feature to gate at all.

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
    # urgent case. Resolved once, up front, so both the recommendations
    # engine and the "My team" panel's own lineup read the same number.
    resolved_priority = my_priority if my_priority is not None else loaded.waiver_priority

    # The "My team" panel is deliberately the CURRENT-roster optimal lineup
    # (build_week_brief), not the post-pickup one `ffbot.gameplan` computes
    # for Recommendations -- the two can legitimately disagree (e.g. an
    # incumbent K still shown starting in My team while a bye-week streamer
    # is recommended above it), which is the intended read: "what's true
    # right now" vs. "what to do about it."
    brief = week.build_week_brief(
        players, cfg.roster_positions, week_num, cfg, weekly, stadiums,
        board=board, league_rosters=league_rosters, opponent_starters=loaded.opponent_starters or None,
    )
    if commit_lineup and not live_slots:
        rs.save_lineup_state(lineup_state_path, brief.lineup)

    cause = {p.player_id: r for p, r in brief.lineup.benched_for_cause}

    result: dict = {
        "week": brief.week,
        "week_source": week_source,
        "refreshed": refreshed,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Live-projection/roster/league-rosters alerts (e.g. a Sleeper fetch
        # failure) come first -- a data-source problem is more urgent than
        # an ordinary roster note.
        "alerts": (
            list(loaded.projection_alerts)
            + list(loaded.roster_source_alerts)
            + list(loaded.league_rosters_alerts)
            + list(loaded.game_conditions_alerts)
            + list(loaded.standings_alerts)
            + list(loaded.opponent_alerts)
            + list(loaded.board_alerts)
            + list(loaded.scoring_alerts)
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

    if show_waivers:
        plan = gameplan.build_gameplan(
            loaded, week_num, players, my_priority=resolved_priority, weeks_in_season=weeks_in_season,
        )
        result["opponent"] = _opponent_json(loaded, plan)
        result["moves"] = {
            "start_sit": [_swap_line_json(l) for l in plan.start_sit],
            "unfilled_slots": list(plan.unfilled_slots),
            "adds": [_adddrop_json(r) for r in plan.adds],
            "claims": [_adddrop_json(r) for r in plan.claims],
            "ir_stash": [
                {"add_name": c.add_name, "position": c.position, "value": c.value, "reason": c.reason}
                for c in plan.ir_stash
            ],
            "missing": list(plan.missing),
            "notes": list(plan.notes),
        }

    return result
