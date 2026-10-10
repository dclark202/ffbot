# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ffbot` is a local web GUI (`scripts/gui.py`, port 8321) live-synced to one Sleeper fantasy football league: a draft room with live pick sync, and a read-only weekly manager that auto-loads the current week's real roster/lineup/waiver state. In season, the scheduled checks (`scripts/autorun.py`) and the push notifications they send are the surface actually in use. Terminal CLIs (`scripts/draft.py`, `scripts/week_report.py`) share the identical engine, draft log, and lineup state — see [docs/REFERENCE.md](docs/REFERENCE.md) for those and the full config reference; [docs/GUIDE.md](docs/GUIDE.md) is the day-to-day walkthrough. Everything is read-only against Sleeper's public, unauthenticated API — permanently advisory, since Sleeper has no write endpoint at all. The shipped `config.yml` is live-by-default (`projection_source`/`roster_source`/`standings_source`/`league_rosters_source`/`board_points_source` all set to `sleeper`); the code-level dataclass defaults in `ffbot/config.py` are offline, used only when a key is deleted entirely.

## Commands

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on macOS/Linux

pytest                                            # run the full suite
pytest tests/test_lineup.py                       # one file
pytest tests/test_lineup.py::test_name -v          # one test

.venv/Scripts/python scripts/whoami.py --username <you>   # discover your Sleeper league_id/roster_id, no credentials needed
.venv/Scripts/python scripts/init_league.py --username <you>   # bootstrap config.local.yml + league.yml from Sleeper

.venv/Scripts/python scripts/gui.py --slot 4                         # web GUI, http://127.0.0.1:8321/
.venv/Scripts/python scripts/draft.py --slot 4                       # live draft assistant (terminal)
.venv/Scripts/python scripts/draft_export.py --board rankings.csv    # pre-draft board -> Sleeper pre-draft-rankings export
.venv/Scripts/python scripts/week_report.py --week 3 --stream K DEF --waivers   # weekly brief (terminal; see docs/GUIDE.md)
.venv/Scripts/python scripts/grade_week.py --week 3 --fetch               # grade that week's projections + adjustments against real points (--all for the season)

.venv/Scripts/python scripts/autorun.py --dry-run                        # print this week's scheduled-check schedule
.venv/Scripts/python scripts/schedule_autorun.py register --dry-run      # print the Task Scheduler / cron command that would register it

.venv/Scripts/python scripts/make_training_pack.py --count 30 --export training/pack.html   # synthetic draft situations for a human reviewer (see docs/dev/TRAINING.md)
.venv/Scripts/python scripts/training_report.py --pack training/packs/pack.json --responses training/responses/pack-dad.json   # grade their answers
```

No lint/format tooling is configured in this repo.

See [docs/REFERENCE.md](docs/REFERENCE.md#data-sources) for a single-page inventory of every information source behind a draft or weekly recommendation — every live fetch, local file, and human-research input, with the `config.yml` toggle for each.

## Architecture

- **`ffbot/sleeper/`** — the read-only, unauthenticated HTTP client for Sleeper's public API (`SleeperClient`, stdlib `urllib` only). Documented endpoints are stable; two undocumented ones (season projections/ADP, ownership%) are unsupported by Sleeper and every caller must treat a fetch failure as "no data this run." Per-endpoint TTL cache (`cache.py`); `draft()`/`draft_picks()` are never cached. `models.py` translates Sleeper's word-based injury status onto this repo's single-letter vocabulary.
- **`ffbot/live/` + `ffbot/markets/`** — the weekly outside-data clients: `live/schedule.py` (real NFL schedule, always refetched), `live/weather.py` (Open-Meteo forecast), `live/conditions.py` (merges both plus Kalshi odds with `weekly/week-NN.yml` research, field by field), `live/form.py` (the FORM signals from Sleeper's realized weekly stats). `markets/kalshi.py`/`kalshi_nfl.py` are the raw and NFL-semantic Kalshi clients (per-player prop markets gated on `use_untested_features`).
- **`ffbot/availability.py` + `ffbot/demand.py`** — pure derivations over payloads `report.load_everything` fetches: each unrostered player's waivers-vs-free-agent status, and what other managers are doing about him (rivals' failed claims, trending adds, ownership delta).
- **Core engine** — `ffbot/models.py` (the `Player` shape and status/slot vocabulary), `ffbot/lineup.py` (the deterministic, exactly-optimal bipartite-matching lineup optimizer), `ffbot/policy.py` (drop guardrails and the claim noise floor), `ffbot/form.py` (the five FORM signals as pure math, shared by the live feed and `ffbot/history/signals.py`).
- **Weekly path** — `ffbot/week.py` (status overrides, weather/Vegas/venue adjustments, waiver scan, claim economics, IR moves), `ffbot/gameplan.py` (the unified weekly recommendations engine behind the GUI, the CLI and every push), `ffbot/denial.py` (tactical denial), `ffbot/roster_source.py` + `ffbot/sleeper_roster.py` (offline and live roster routes), `ffbot/report.py` (`load_everything`, the shared loader with one alert list per live seam), `ffbot/webapi.py` (GUI JSON serializers), `ffbot/week_log.py` (the per-run JSON record in `weekly/reports/`), `ffbot/week_grade.py` (grades those records against real points — descriptive only), `ffbot/notify.py` (push transport, formats nothing), `ffbot/research.py` (unattended headless-Claude research into `weekly/week-NN.yml`).
- **Draft path** — `ffbot/board.py` (frozen pre-draft board, VOR/tiers, the live points overlay), `ffbot/draft.py` (pick math, ADP survival, `DraftState`), `ffbot/edge.py` (contrarian valuation terms), `ffbot/draft_ui.py` (pure state machine shared by terminal and GUI), `ffbot/draft_sync.py` (live Sleeper pick sync), `ffbot/names.py` (two deliberately separate matchers), `ffbot/intel.py` (researched `draft/intel.yml`), `ffbot/draft_report.py` (the per-draft JSON record).
- **`ffbot/history/` + `ffbot/backtest/`** — point-in-time replay against real NFL seasons. See [docs/dev/BACKTEST.md](docs/dev/BACKTEST.md) for the design and [docs/dev/SPICE.md](docs/dev/SPICE.md) for the evidence behind each dial's shipped value.
- **`ffbot/training.py` + `ffbot/training_export.py`** — synthetic draft situations exported as a standalone HTML page for a human reviewer ([docs/dev/TRAINING.md](docs/dev/TRAINING.md)). Every finding is a hypothesis until `scripts/backtest_draft.py` agrees.
- **`scripts/` + `web/`** — thin shells around the packages above. `scripts/autorun.py` owns the schedule and builds every push body; `scripts/schedule_autorun.py` registers it with Task Scheduler/cron. `web/draft_render.js` is shared by the live draft room and the offline training reviewer.

Module docstrings carry the detail — read the file itself for exactly how a given piece works. The incident behind each dated rule below is written up in [docs/dev/INSEASON-FINDINGS.md](docs/dev/INSEASON-FINDINGS.md).

## Design invariants worth preserving

### Config and structure

- **Config, not code, for tuning.** New thresholds/weights belong in `config.yml` + the corresponding dataclass in `ffbot/config.py`.
- **`config.yml` is never rewritten by code.** It's hand-narrated with comments a YAML round-trip would destroy. Anything that writes settings (the GUI's Settings page) writes the gitignored `config.local.yml` overlay (`_deep_merge`d on top at load).
- **`use_untested_features` gates FEATURES, never intensity, and the gate runs LAST.** `SEASON_UNTESTED_DIALS`/`DRAFT_UNTESTED_DIALS` are exactly `{k : level-3 value == 0 and level-4 value != 0}` (`tests/test_config.py::TestBaselineAndUntestedToggle` derives and asserts it). In `_resolve_block` the gate applies AFTER explicit per-key overrides, so an unticked box forces those dials to 0.0 even over a slider value. Don't fold an evidence-backed dial into the untested set.
- **`scripts/draft.py`, `scripts/week_report.py`, and `scripts/gui.py` must stay importable and runnable with no network at module level.** `ffbot.sleeper`/`ffbot.sleeper_roster`/`ffbot.projections.sleeper` are imported lazily inside the function that needs them. `week_report.py`'s research step stays outside the script entirely (`/gameday`, or `autorun.py` → `ffbot/research.py`); the report only reads the file.
- **`scripts/gui.py` is single-threaded on purpose.** One shared, mutable `UiState` with no lock; `http.server.HTTPServer` serializing every request is what keeps `DraftState` main-thread-only. Don't switch to a threading server without real synchronization.
- **Every live network call takes an injectable `opener`/client, and every fetch failure degrades gracefully with a surfaced alert — never a silent success and never a crash.** A test proving this structurally must exist for any new live seam (pattern: `tests/test_history_index.py::TestAsOfLeakageGuarantee`).

### The lineup optimizer

- **`ffbot/lineup.py` must stay exact and deterministic:** (1) exact optimality, (2) stable output (ties break on player name), (3) the flex-seating rule, (4) minimal-move preference among assignments the seating rule cannot separate. A `Player.game_locked` player is held where he is — a locked starter's slot is pinned, a locked bench/IR player is never a candidate, neither produces a `Move` — and with nobody locked the output is identical to the lock-free algorithm (`tests/test_lineup.py::TestGameLocks`).
- **Seating is a decision, and the seating rule outranks minimal-move.** Every perfect matching of the chosen starters scores identically, but the flex is the only versatile slot and each player locks at his own kickoff. `lineup._flex_seating_order`: an early-kickoff player is kept out of the flex first, then the flex goes to the most replaceable starter. Minimal-move alone ratified whatever Sleeper's pre-seated roster had; it survives only as the third sort key and via slots comparing by NAME. A move between two starting slots is labelled "no points change".
- **Season-long valuation always passes `week=None`** to `optimize()`. `week.waiver_candidates()`/`rank_streamers()` accept a real `week` only to zero a candidate's *this-week* contribution on bye — the rest-of-season side must never be zeroed by it. Weekly lineup decisions (`week.build_week_brief`) are the one place a real week number is required.

### In-season valuation and pricing

- **In-season valuation reads only live weekly and rest-of-season projections, never the draft board.** `gameplan.valuation_pool` is the one pool every weekly consumer reads; under `projection_source: sleeper` it is the ROS board built with `rescale_board_points(..., live_only=True)`. A failed ROS fetch skips waiver/streaming valuation with a note, never falls back to draft-board numbers; a rostered player with no live projection lands in `roster_board_keys`' `missing`.
- **A weekly projection shown to a human is the number the Sleeper app shows, and every change we make to it is named.** Sleeper rows carry raw stats and are scored by `scoring.score_sleeper_stats` against the league's own `scoring_settings`. `PlayerMetrics` carries `sleeper_proj` beside `week_proj` and each step's delta (`week.adjusted_players_with_breakdown`). Real points for started games (`live_points`) are descriptive only.
- **Live forecast weather outranks researched weather.** `live.conditions.merge_conditions` merges field by field: the Open-Meteo forecast wins for wind/precip/gusts/temp whenever it has a reading (research fills gaps, with an alert on large disagreement); research wins for everything else. Pinned by `tests/test_live_conditions.py::TestMergeConditions::test_jax_cle_2026_week_1_regression`.
- **A waiver claim's cost is ABSOLUTE, never a fraction of the gain it buys.** `week.priority_option_cost` prices a priority slot from the slot and `week.decision_scale`; `claim_cost` must not depend on `gain` (the fractional form made HOLD PRIORITY unreachable). `tests/test_week.py::TestClaimVerdictEconomics` asserts gain-invariance and reachability. A HOLD verdict is charged NOTHING, so the row is demoted to an ordinary add rather than deleted.
- **Only a player ON WAIVERS is ever charged waiver priority.** `ffbot/availability.py` derives status each run by Sleeper's own rules (on waivers from his kickoff until the league's next weekly run; a dropped player until his clear date; otherwise a free agent). `week.acquisition_verdict` is the one place that becomes economics: free agent → kind `"add"` at zero cost; waiver player → `"claim"` or `"wait"`. `Availability.prior_kickoffs` feeds ONLY that rule, never `game_states()`. Unknown status prices every add as a claim, with an alert. "Act now" is decided by `AddDropRec.kind`, never by a `claim_note`'s wording.
- **One executable move per slot; the rest are `backups`, never rows of their own.** `gameplan.fold_backups` keeps the best row of a group (same streaming slot, or same position spending the same drop) and attaches the others as typed `AddDropRec.backups`; the acceptance loop consumes a stream row's incumbent as a drop key. Claims at different positions sharing a drop stay separate rows by design. `tests/test_gameplan.py::TestOneExecutableMovePerSlot`.
- **The noise floor is in points per week, and it stays ON.** `policy.can_claim(week_gain, ros_gain_per_week, …)` clears a move when either horizon beats `noise_floor_weight * decision_scale / predictiveness[pos]`. A forced need (K/DEF on bye/OUT) is exempt; a floored row is noted, not dropped; a sub-floor start/sit swap keeps the optimizer's order but is labelled a toss-up — on the push row too, and including a swap routed through a slot shift (`pair_moves` follows the shift chain). Re-grading may move the value; the manager's standing instruction is that it never turns off.
- **No displayed recommendation number is the ros/week blend.** `value`/`net` mix a rest-of-season TOTAL with ONE week's points; it is the internal ranking key, labelled `rank key … (blend)`. Everything shown is points per week (`DecisionMetrics.week_gain`, `ros_gain_per_week`).
- **A streaming position's gain is weighted less on rest-of-season value, but never zero** (`SeasonConfig.stream_ros_blend`, 0.25 vs. `ros_blend` 0.5). 0.0 was tried and inverts the board.
- **Drops are guarded by explicit policy, not a confirmation step.** New irreversible actions get the same: a `Verdict`-returning policy function with the reason surfaced. `policy.can_drop` refuses a `game_locked` player.
- **IR eligibility is the league's `reserve_allow_*` setting, read from Sleeper's own status word, and an IR-parked player is never a drop.** `sleeper_roster.apply_sleeper_identity` stamps `Player.ir_eligible` once from the RAW `injury_status`; a researched override never re-stamps it. `week.plan_ir_moves` runs before any pricing, so an add into the freed spot needs no drop and carries `AddDropRec.via_ir`; `week.ranked_droppable` excludes IR occupants.
- **A recommendation's metrics are typed fields, never re-parsed from `reason` text.** New numbers go on `gameplan.PlayerMetrics`/`DecisionMetrics` in a stated unit and into `webapi`'s serializers, which `ffbot/week_log.py` reuses rather than copies.

### Descriptive-only signals

Each of these is attached to recommendations and logged, and nothing reads it back into a valuation, ranking, or the optimizer. Each names the test that guards it.

- **Season points-to-date** (`season_stats_source`, `PlayerMetrics.season_ptd`) — the one in-season source of REALIZED results; pricing it would double-count a live `ros_board`. `tests/test_gameplan.py::TestSeasonPointsToDateIsDescriptiveOnly`; `tests/test_projections_actuals.py::TestSeasonToDateNeverReachesTheBacktest`.
- **League demand** (`ffbot/demand.py` → `SpeculativeCandidate.demand`, `PlayerMetrics.demand`) — no `net`/`value`/`gain`/`urgency`/`denial_value` reads it; the endpoints keep no archive, so it could only ever be graded forward. `DemandSignal.is_retrospective` carries the timing split in the type. `tests/test_gameplan.py::TestWaiverDemandIsDescriptiveOnly`.
- **A speculative row is not a move.** `GamePlan.speculative` rows carry no `net`, never enter `post_roster`, never consume a drop key or open spot, and never reach a push. A row must be genuinely below the drop it would cost (`week._is_below_your_worst`), is priced against your worst player with `ranked_droppable(ignore_game_locks=True)` (executable paths keep the default), and must be for a player who has not yet played (`week._already_played`). `tests/test_demand.py::TestALiveSlateDoesNotChooseTheDrop`.
- **A projection grade proposes; it never tunes.** `scripts/grade_week.scheduled_grade` runs Tuesday mornings; evidence is counted per GAME (`week_grade.game_key`); nothing in `ffbot/` imports `week_grade` and nothing writes configuration (`tests/test_week_grade.py::TestDescriptiveOnly`). A human moves the dial.
- **The draft table's pick confidence (`p_best`) is presentational only.** `ffbot/draft.py` must never reference it, and its temperature (`DraftConfig.pick_confidence_scale`) stays an ABSOLUTE scale in season points (`tests/test_edge.py::TestPickConfidence::test_scale_is_absolute_not_relative`).

### Dials and their inputs

- **A dial that is set but has no input must say so.** `report._INTEL_BACKED_DIALS` pairs each weight with the `WeeklyPlayerIntel` field it reads; `_intel_coverage_alerts` surfaces zero AND partial coverage (`week.usage_score(None)` is 0.0, not a neutral 0.5, so coverage is itself a ranking effect). `tests/test_intel_coverage.py` makes a new unregistered dial fail.
- **A Validated dial must have a wire, and the wire must be one implementation.** The five FORM dials (`usage`/`momentum`/`divergence`/`volatility`/`upside_lean`) were inert for a season until `ffbot/live/form.py` (`form_source: sleeper`) fed them; research still wins field by field (`report._merge_form_scores`). The math lives in `ffbot/form.py` and is shared with `ffbot/history/signals.py` (`tests/test_live_form.py::TestBothFeedsShareOneImplementation`). `form.MIN_GAMES` is 3, so they are silent through week 3 by design. Ties in `percentile_rank_within_position` share the average rank.
- **A candidate the waiver scan discards must leave a record.** `week.ScanTrace` (`None` is bit-identical to not passing one); `trace.scanned == priced + zero_gain + len(noise_floored)` is asserted. The `gain <= 0` bar itself STAYS — a bench-quality add is `<= 0` by construction.
- **Forward-logging a signal never waits on that signal's weight.** `SeasonConfig.kalshi_forward_log` writes `data/kalshi_log/` while `kalshi_weight` is 0.0 and merges nothing into valuation.

### The push

- **The push IS the product; the GUI is secondary.** The manager reads ntfy notifications and executes by hand in the Sleeper app. A change that surfaces only in the GUI or the CLI has not reached the decision — `plan.notes` and `_all_alerts` never reach a push. Bodies are built in `scripts/autorun.py`; `ffbot/notify.py` is transport.
- **A push body is exactly four sections and nothing else: `START/SIT`, `ADD/DROP`, `WAIVERS`, `MONITOR` (`_SECTION_ORDER`).** Anything else worth saying is a ROW inside a section or it is gone. All four empty means an EMPTY BODY and the title alone (`notify.py` sends a zero-width space so ntfy doesn't substitute "triggered"). The one exception: a run on broken inputs (research FAILED, a live feed fell back) says so in the TITLE via `_title_suffix`.
- **A quiet check still sends, because the ABSENCE of a message is the failure signal** (`heartbeat_message`/`waiver_heartbeat`/`post_waiver_heartbeat`/`look_heartbeat`, under `notify.heartbeat`). `actionable_summary` decides whether to WAKE someone and stays conservative: decision sections render FIRST and MONITOR is appended only once they are non-empty. A test of this must pass a real `cfg` — `cfg=None` skips the section and passes vacuously.
- **MONITOR is at most three near misses you could add right now** (`monitor_lines`, shared by every message): a claim, or a free agent with known availability — never a `wait` row, never a speculative or demand row.
- **A scheduled check has a purpose, and its message is shaped by it (`Trigger.kind`).** Pre-kickoff is lineup-first. Tuesday's waiver check: your priority, each claim with its backups, what to leave for free agency — never a lineup line. Wednesday's free-agent check: what the run did with your claims, and free agents worth adding with where each starts. Every slot lives in `config.yml`'s `autorun:` block; CLI flags override.
- **A due-diligence look (`kind == "look"`, `autorun.looks`) is a look at the ROSTER that researches first, never a research report.** It sends the same lineup-first body a pre-kickoff check does. `looks` is `{weekday: local hour}` (shipped `{fri: 19, sat: 18}` — after the last game-status report, and after the Saturday elevation deadline). `research.injury_report_*` is the legacy single slot, read only when `looks` is empty.

### Unattended research

- **Research may write only the week file, and only an official source can set a `status`.** `ffbot/research.py` runs headless Claude Code under `--permission-mode dontAsk` with an allow-list of web search/fetch, Read, and Edit scoped to `weekly/week-NN.yml` (`Edit(path)` covers Write; the CLI rejects a `Write(path)` rule) — never widen it, never use `bypassPermissions`. The official-source rule (`ResearchConfig.official_source_domains`) is enforced in code after every run; a write that fails validation is rolled back byte-for-byte; a slot pass never deletes another slot's entries. `tests/test_research.py`.

### Draft and board

- **A `Board`'s `points` overlay (`_apply_points_overlay`/`rescale_board_points`) must run AFTER `apply_league_scoring`**, never folded into the row merge ahead of it — otherwise the recompute clobbers the live number back to the frozen one (regression test in `tests/test_board.py`).
- **Pre-draft ID matching vs. live TUI search are different problems** (`ffbot/names.py`) — don't collapse them into one matcher.

### History and backtest

- **Historical replay must go through `ffbot.history.index.as_of()`** — the single point-in-time boundary, which never fetches a results-bearing source. No backtest code reads `data/history/*.csv` directly.
- **A `SignalProvider` (`ffbot/history/signals.py`) must never be folded into `as_of()`.** A provider needing `stats_player_week` stays outside the boundary and merges in via `WeekSnapshot.with_signals()`.
- **`ffbot/backtest/` reads realized outcomes only through `ffbot.history.actuals.week_actuals`**, never `WeekSnapshot.game_rows` directly.
- **Any calibration used for historical projections must be fit on seasons other than the one being graded.** `ffbot.history.projections.ecr_projections` raises rather than documents it.
- **`scripts/backtest_season.py`'s agent − control delta cannot grade a waiver dial** — control reads the same `cfg`.

## Project status

**In the 2026 regular season** (week 1 kicked off 2026-09-09). The draft is done — `draft/reports/draft-1393066447571255296.json` is the real one — and the draft-path queue in [docs/dev/TRAINING-FINDINGS.md](docs/dev/TRAINING-FINDINGS.md) is parked until next August.

Live-season work is logged in [docs/dev/INSEASON-FINDINGS.md](docs/dev/INSEASON-FINDINGS.md): one dated entry per incident (what the push said, the trace, what changed), then the open queue (W1–W9) and a "not changing" list. Add to it rather than to this file; a new rule earns a line above only once it is an invariant with a guard test.

Built and tested against the live Sleeper API: the optimizer and drop/waiver policy guardrails, the draft assistant, the weekly manager, the GUI, the scheduled-task runner with unattended research, and B1–B15 of the backtest plan (B1–B10 and B15 in [docs/dev/BACKTEST.md](docs/dev/BACKTEST.md), B11–B14 in TRAINING-FINDINGS). The user-facing spice ladder is gone as of B10: the old level-3 preset is the fixed baseline (`SEASON_BASELINE`/`DRAFT_BASELINE`), every dial has a slider on the GUI Settings page, and one `use_untested_features` checkbox per block gates the dials with no backtest evidence. The 1–4 presets survive internally for `scripts/backtest_*.py`, `mock_draft.py --bot-spice`, and `make_training_pack.py --my-spice`; a `spice_level` key still loads but is deprecated. Trade support has no design yet.
