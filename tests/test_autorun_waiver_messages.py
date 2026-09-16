"""The two waiver-cycle checks' messages (`scripts/autorun.py`): the Tuesday
waiver-claims check and the Wednesday free-agent check.

2026-09-15's push led with three lineup moves days before any game, then
three defenses typed as free agents against one drop. The manager's call:
claims first, the fallback on the claim, no lineup lines at all, an explicit
"nothing worth a claim" over silence, and a Wednesday check for the free
agents once the run has processed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ffbot.availability import WAIVERS, Availability, ClaimOutcome, PlayerAvailability
from scripts import autorun
from scripts import week_report as wr_module

CLEARS = datetime(2026, 9, 16, 7, 5, tzinfo=timezone.utc)


def _row(kind, name, net, *, pos="DEF", drop="Detroit Lions", week_gain=3.6, ros=0.0,
         backups=(), on_waivers=True, if_clears=""):
    return SimpleNamespace(
        kind=kind, position=pos, add_name=name, add_team="", drop_name=drop, net=net,
        claim_note="", reasons=(),
        decision=SimpleNamespace(week_gain=week_gain, ros_gain_per_week=ros),
        availability=PlayerAvailability(status=WAIVERS, clears_at=CLEARS) if on_waivers else PlayerAvailability(),
        if_clears=SimpleNamespace(text=if_clears) if if_clears else None,
        backups=tuple(SimpleNamespace(add_name=b, add_team="") for b in backups),
    )


def _trigger(kind):
    if kind == "waiver":
        return autorun.Trigger(id="pre_waiver_2026-09-15", due_at=datetime(2026, 9, 15, 20), grace_minutes=720,
                               label="waiver claims (tue 20:00)", kind="waiver")
    return autorun.Trigger(id="post_waiver_2026-09-16", due_at=datetime(2026, 9, 16, 7), grace_minutes=720,
                           label="free-agent check (wed 07:00)", kind="post_waiver")


def _loaded(**kw):
    base = dict(
        waiver_priority=7, availability=None, availability_source="off", claim_outcomes=[],
        projection_source="sleeper", roster_source="sleeper", slots_source="sleeper",
        league_rosters_source="sleeper", season_ptd_source="off", ros_board=None, board=None,
        cfg=SimpleNamespace(league=None),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run(waivers=(), start_sit=(), loaded=None):
    brief = SimpleNamespace(lineup=SimpleNamespace(assignments=[], moves=[]))
    plan = SimpleNamespace(start_sit=list(start_sit), current_plan=brief.lineup)
    return wr_module.ReportRun(
        week=2, loaded=loaded if loaded is not None else _loaded(), brief=brief,
        waivers=list(waivers), sections=["WEEK 2"], plan=plan,
    )


def _cfg(heartbeat=True, post_waiver=True):
    return SimpleNamespace(
        notify=SimpleNamespace(min_waiver_net=2.0, heartbeat=heartbeat),
        draft=SimpleNamespace(num_teams=12),
        autorun=SimpleNamespace(post_waiver_enabled=post_waiver),
    )


LINEUP = SimpleNamespace(
    kind="swap", slot="RB", slot_display="RB", start_name="Quinshon Judkins", bench_name="D'Andre Swift",
    text="RB: Start Quinshon Judkins (CLE) — Bench D'Andre Swift (CHI)",
)
ADD_START = SimpleNamespace(
    kind="add_start", slot="DEF", slot_display="DEF", start_name="Green Bay Packers", bench_name="Detroit Lions",
    text="DEF: Add & start Green Bay Packers — Drop Detroit Lions (DET)",
)


class TestWaiverClaimsMessage:
    def test_leads_with_priority_and_the_claim_with_its_backups(self):
        claim = _row(
            "claim", "Kansas City Chiefs", 2.8, backups=("Green Bay Packers", "San Francisco 49ers"),
            if_clears="if it clears: start at DEF over Detroit Lions (+3.6 this week)",
        )
        title, body = autorun.notification_for(_run([claim]), _trigger("waiver"), _cfg(), {})
        lines = body.split("\n")
        assert title == "ffbot W2: waiver claims (tue 20:00)"
        assert lines[0].startswith("WAIVER CLAIM")
        # One line per TRANSACTION: what to claim, what it costs, what it
        # gains. The clear time and the if-it-clears consequence live in the
        # report file -- they are derivation, not instruction.
        assert lines[1] == "  CLAIM DEF Kansas City Chiefs  DROP Detroit Lions  +3.6"
        # The if-it-clears consequence is deliberately NOT on the push: on
        # Tuesday the claim has not processed, so it is a hypothetical, and
        # a hypothetical is derivation. It stays in the report file.
        assert "if it clears" not in body
        # The backups a claim can fall back to are still named -- only ONE of
        # the group is executable -- but on their own indented line, not
        # trailing the instruction.
        assert lines[2] == "    alt: Green Bay Packers, San Francisco 49ers"

    def test_carries_no_lineup_lines_even_with_moves_to_make(self):
        claim = _row("claim", "Kansas City Chiefs", 2.8)
        _, body = autorun.notification_for(_run([claim], start_sit=[LINEUP]), _trigger("waiver"), _cfg(), {})
        assert "Lineup" not in body and "Judkins" not in body

    def test_what_to_leave_for_free_agency_follows_the_claims(self):
        rows = [
            _row("claim", "Kansas City Chiefs", 2.8),
            _row("wait", "Green Bay Packers", 1.9, week_gain=3.7),
            _row("wait", "Tampa Bay Buccaneers", 1.8, week_gain=4.6),
        ]
        _, body = autorun.notification_for(_run(rows), _trigger("waiver"), _cfg(), {})
        wait = next(line for line in body.split("\n") if line.startswith("Wait for free agency"))
        assert "clears" in wait
        assert wait.endswith(": DEF Green Bay Packers (+3.7 this wk), DEF Tampa Bay Buccaneers (+4.6 this wk)")

    def test_a_free_agent_worth_adding_tonight_is_still_pushed(self):
        # A bye team's player is a free agent on a Tuesday; worth the bar, he goes out.
        add = _row("add", "Bye Team Defense", 5.0, on_waivers=False)
        _, body = autorun.notification_for(_run([add]), _trigger("waiver"), _cfg(), {})
        assert "ADD DEF Bye Team Defense  DROP Detroit Lions" in body

    def test_nothing_over_the_bar_is_an_explicit_no_claim_message(self):
        run = _run([_row("claim", "Kansas City Chiefs", 0.6, week_gain=1.2)])
        title, body = autorun.notification_for(run, _trigger("waiver"), _cfg(), {})
        assert title == "ffbot W2: waiver claims (tue 20:00) -- nothing worth a claim"
        assert body.startswith("No claim worth your priority tonight.")
        assert "Rolling priority: you're 7th of 12" in body
        # The near miss now lives under MONITOR, which is exactly what that
        # section is for: close to a bar this week, could clear it next.
        assert "MONITOR" in body
        assert "DEF Kansas City Chiefs  +1.2  under the 2.0 bar" in body
        assert "The free-agent check after the run will say who to pick up." in body
        # Feed health is an alarm, not a status line: an all-clear every
        # week is read once and then ignored, which is how a real
        # degradation gets missed.
        assert "every Sleeper feed answered" not in body
        assert "Checked " in body

    def test_the_no_claim_message_does_not_promise_a_check_that_is_off(self):
        _, body = autorun.notification_for(_run(), _trigger("waiver"), _cfg(post_waiver=False), {})
        assert "free-agent check" not in body

    def test_heartbeat_off_keeps_a_quiet_tuesday_silent(self):
        assert autorun.notification_for(_run(), _trigger("waiver"), _cfg(heartbeat=False), {}) is None

    def test_a_broken_research_pass_is_reported_even_when_quiet(self):
        from ffbot.research import ResearchResult

        failed = ResearchResult(ok=False, alerts=["research failed (exit 129: ...)"])
        _, body = autorun.notification_for(_run(), _trigger("waiver"), _cfg(heartbeat=False), {}, research=failed)
        assert body.startswith("Research FAILED")


class TestFreeAgentMessage:
    OUTCOME = ClaimOutcome(
        add_name="Kansas City Chiefs", drop_name="Detroit Lions", status="complete", note="", processed_at=CLEARS,
    )

    def test_claim_outcomes_then_free_agent_adds_with_where_they_start(self):
        add = _row("add", "Green Bay Packers", 2.6, week_gain=3.7, on_waivers=False, backups=("San Francisco 49ers",))
        run = _run([add], start_sit=[ADD_START], loaded=_loaded(claim_outcomes=[self.OUTCOME]))
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        lines = body.split("\n")
        assert title == "ffbot W2: free-agent check (wed 07:00)"
        assert lines[0] == "WAIVER RESULTS"
        assert lines[1] == "  Your claim for Kansas City Chiefs (dropping Detroit Lions): processed"
        # The add and the lineup move it causes are ONE visit to the
        # Sleeper app, so they are one line -- and the START must not be
        # repeated under START/SIT.
        assert "ADD/DROP" in lines
        assert (
            "  ADD DEF Green Bay Packers  DROP Detroit Lions  START at DEF over Detroit Lions  +3.7"
            in lines
        )
        assert "    alt: San Francisco 49ers" in lines
        assert "START/SIT" not in lines
        assert "Lineup" not in body

    def test_claims_and_waits_are_not_wednesdays_business(self):
        run = _run([_row("claim", "Kansas City Chiefs", 2.8), _row("wait", "Somebody", 2.5)])
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title.endswith("nothing worth adding") and "CLAIM" not in body

    def test_a_processed_claim_alone_is_worth_a_push(self):
        run = _run(loaded=_loaded(claim_outcomes=[self.OUTCOME]))
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title == "ffbot W2: free-agent check (wed 07:00)" and body.startswith("WAIVER RESULTS")

    def test_nothing_to_add_is_an_all_clear_that_says_what_it_looked_at(self):
        avail = Availability(now=CLEARS, cycle_start=CLEARS, next_run=CLEARS + timedelta(days=7))
        run = _run(
            [_row("add", "Green Bay Packers", 0.9, week_gain=1.1, on_waivers=False)],
            loaded=_loaded(availability=avail, availability_source="sleeper"),
        )
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title.endswith("-- nothing worth adding")
        # "No claim of yours was processed" answered a question nobody
        # asked and read as though something had failed.
        assert "No claim of yours was processed" not in body
        # The near miss is what MONITOR is for -- and it must NOT have made
        # this check actionable; the title still says nothing worth adding.
        assert "MONITOR" in body
        assert "  DEF Green Bay Packers  +1.1  under the 2.0 bar" in body

    def test_a_completely_quiet_wednesday_is_just_the_title_and_a_timestamp(self):
        """With nothing processed and nothing to add, the TITLE already says
        "nothing worth adding" -- repeating it in the body only pushed real
        content down the screen, and the body must not open with a blank
        line where that sentence used to be."""
        title, body = autorun.notification_for(_run(), _trigger("post_waiver"), _cfg(), {})
        assert title.endswith("-- nothing worth adding")
        assert "No claim of yours" not in body
        assert "Nothing worth a free-agent add." not in body
        assert body.startswith("Checked ")

    def test_heartbeat_off_keeps_a_quiet_wednesday_silent(self):
        assert autorun.notification_for(_run(), _trigger("post_waiver"), _cfg(heartbeat=False), {}) is None


# The 2026-09-16 row: worse than the man he'd cost, wanted by three rivals.
def _spec(retrospective=True, name="Kaelon Black"):
    from ffbot.demand import DemandSignal

    return SimpleNamespace(
        add_name=name, position="RB", team="SF",
        week_proj=7.0, ros_proj_per_week=4.9, ros_proj_total=83.7, on_bye=False,
        open_spot=False,
        drop_name="Tyjae Spears", drop_team="TEN", drop_position="RB",
        drop_week_proj=7.7, drop_ros_proj_per_week=7.3, drop_hold_margin=-1.0,
        drop_reason="worst hold value on your roster",
        week_delta=-0.7, ros_delta_per_week=-2.4,
        filtered_by="gain<=0", gain=-1.2,
        demand=(
            DemandSignal(
                "rival_failed_claims", 3.0, "claims", "waiver run 2026-09-16", "",
                "3 rivals claimed him at the last run", True,
            ) if retrospective else
            DemandSignal(
                "sleeper_trending_add", 41200.0, "leagues", "48h", "",
                "41,200 leagues added him in 48h", False,
            ),
        ),
        availability=None,
    )


class TestSpeculativeNeverTriggersANotification:
    """A speculative row may RIDE ALONG on a message that is already going
    out; it may never be the reason one is sent.

    `actionable_summary` is the function that decides whether to wake a
    human, and a speculative row is by construction below the bar the
    +0.6-point defense cleared in week 1. Giving it that power would reopen
    exactly that wound.
    """

    def test_a_speculative_only_run_is_quiet(self):
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=False)]
        assert autorun.actionable_summary(run, min_waiver_net=2.0) == []

    def test_it_rides_along_on_the_tuesday_heartbeat(self):
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=False)]
        _title, body = autorun.waiver_heartbeat(run, _trigger("waiver"), _cfg())
        assert "MONITOR" in body
        assert "RB Kaelon Black" in body

    def test_a_retrospective_signal_is_absent_from_the_tuesday_body(self):
        """Tuesday precedes the waiver run, so last week's claim results are
        the ONLY league-specific evidence available -- and citing them there
        would have the message claim knowledge of a run that has not
        happened yet. Filtered on `is_retrospective`, never on the source
        name: matching on text is how a HOLD-PRIORITY row got seated as
        'Add & start'.
        """
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=True)]
        _title, body = autorun.waiver_heartbeat(run, _trigger("waiver"), _cfg())
        assert "Kaelon Black" not in body

    def test_wednesday_carries_it_because_the_run_has_happened(self):
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=True)]
        lines = autorun.monitor_lines(run, _cfg(), pre_run=False)
        assert any("3 rivals claimed him" in l for l in lines)
        assert all(l.startswith("  ") for l in lines), "MONITOR rows sit under their header"

    def test_at_most_two_rows_reach_a_push(self):
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=False, name=f"Player {i}") for i in range(5)]
        lines = autorun.monitor_lines(run, _cfg(), pre_run=True)
        assert len([l for l in lines if "Player " in l]) == 2

    def test_no_speculative_rows_adds_no_lines_at_all(self):
        run = _run(waivers=[])
        run.speculative = []
        assert autorun.monitor_lines(run, _cfg(), pre_run=True) == []
        assert autorun.monitor_lines(run, _cfg(), pre_run=False) == []


class TestThePushIsReadable:
    """The 2026-09-16 rewrite. The old body wrote each recommendation as a
    prose clause with its derivation attached and drew the verdict "I have
    no idea what this means"; these pin the contract that replaced it."""

    def test_sections_appear_in_the_order_the_work_gets_done(self):
        claim = _row("claim", "Kansas City Chiefs", 5.0)
        add = _row("add", "Bye Team Defense", 4.0, on_waivers=False)
        _t, body = autorun.notification_for(_run([claim, add]), _trigger("waiver"), _cfg(), {})
        order = [l for l in body.split(chr(10)) if l and not l.startswith(" ")]
        assert order[:2] == ["WAIVER CLAIM", "ADD/DROP"]

    def test_an_empty_section_is_omitted_not_rendered_as_a_bare_header(self):
        add = _row("add", "Bye Team Defense", 4.0, on_waivers=False)
        _t, body = autorun.notification_for(_run([add]), _trigger("waiver"), _cfg(), {})
        assert "WAIVER CLAIM" not in body
        assert "ADD/DROP" in body

    def test_every_move_line_is_verb_led(self):
        claim = _row("claim", "Kansas City Chiefs", 5.0)
        _t, body = autorun.notification_for(_run([claim]), _trigger("waiver"), _cfg(), {})
        moves = [l.strip() for l in body.split(chr(10)) if l.startswith("  ") and not l.startswith("    ")]
        assert moves and all(
            m.split()[0] in ("ADD", "DROP", "CLAIM", "START", "SIT", "MOVE") or m[0].isupper()
            for m in moves
        )

    def test_the_availability_preamble_is_gone(self):
        """It said the same thing every run, so it was read once and then
        only pushed the real content down the screen."""
        claim = _row("claim", "Kansas City Chiefs", 5.0)
        _t, body = autorun.notification_for(_run([claim]), _trigger("waiver"), _cfg(), {})
        assert "free agency open since" not in body
        assert "unrostered player is a free agent" not in body

    def test_no_line_carries_a_derivation(self):
        """Ownership shares, league-add counts and clear times belong in the
        report file. The push gets points and a short reason."""
        claim = _row("claim", "Kansas City Chiefs", 5.0)
        _t, body = autorun.notification_for(_run([claim]), _trigger("waiver"), _cfg(), {})
        for banned in ("rostered in", "leagues added him", "/wk ROS", "clears "):
            assert banned not in body, banned


class TestTheBodyNeverOpensWithABlankLine:
    """Removing the repeated "nothing worth adding" sentence left the body
    starting on an empty line whenever MONITOR was the first thing in it."""

    def test_a_monitor_only_wednesday_starts_at_the_header(self):
        run = _run([_row("add", "Green Bay Packers", 0.9, week_gain=1.1, on_waivers=False)])
        _t, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert body.startswith("MONITOR")

    def test_a_tuesday_with_content_still_separates_monitor(self):
        run = _run([_row("claim", "Kansas City Chiefs", 0.6, week_gain=1.2)])
        _t, body = autorun.notification_for(run, _trigger("waiver"), _cfg(), {})
        assert not body.startswith(chr(10))
        assert chr(10) + chr(10) + "MONITOR" in body


class TestEveryClockIsTwentyFourHour:
    """A title on a 24-hour clock beside a body on a 12-hour one read as two
    different times and made a check look like it had fired wrongly."""

    def test_the_checked_stamp_has_no_meridiem(self):
        _t, body = autorun.notification_for(_run(), _trigger("post_waiver"), _cfg(), {})
        stamp = next(l for l in body.split(chr(10)) if l.startswith("Checked "))
        assert "AM" not in stamp and "PM" not in stamp
        assert len(stamp.split()[1].split(":")) == 2

    def test_the_helper_is_the_single_source_of_every_clock(self):
        from datetime import datetime as _dt

        assert autorun._clock(_dt(2026, 9, 16, 19, 35)) == "19:35"
        assert autorun._clock(_dt(2026, 9, 16, 7, 5)) == "07:05"
