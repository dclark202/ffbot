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
        assert lines[0] == "Rolling priority: you're 7th of 12"
        assert lines[1].startswith(
            "CLAIM DEF Kansas City Chiefs (drop Detroit Lions) -- +3.6 this wk, +0.0/wk ROS; clears "
        )
        assert "if it clears: start at DEF over Detroit Lions" in lines[1]
        assert lines[1].endswith(" -- backups: Green Bay Packers, San Francisco 49ers")

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
        assert "ADD (free agent) DEF Bye Team Defense (drop Detroit Lions)" in body

    def test_nothing_over_the_bar_is_an_explicit_no_claim_message(self):
        run = _run([_row("claim", "Kansas City Chiefs", 0.6, week_gain=1.2)])
        title, body = autorun.notification_for(run, _trigger("waiver"), _cfg(), {})
        assert title == "ffbot W2: waiver claims (tue 20:00) -- nothing worth a claim"
        assert body.startswith(
            "No claim worth your priority tonight.\nRolling priority: you're 7th of 12\n"
            "Closest call: DEF Kansas City Chiefs for Detroit Lions, +1.2 pts this week "
            "(claim worth +0.6, under your 2-point notify bar)"
        )
        assert "The free-agent check after the run will say who to pick up." in body
        assert "every Sleeper feed answered" in body and "Checked " in body

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
        assert lines[0] == "Your claim for Kansas City Chiefs (dropping Detroit Lions): processed"
        assert lines[1] == (
            "ADD (free agent) DEF Green Bay Packers (drop Detroit Lions) -- +3.7 this wk, +0.0/wk ROS; "
            "starts at DEF over Detroit Lions -- backups: San Francisco 49ers"
        )
        assert "Lineup" not in body

    def test_claims_and_waits_are_not_wednesdays_business(self):
        run = _run([_row("claim", "Kansas City Chiefs", 2.8), _row("wait", "Somebody", 2.5)])
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title.endswith("nothing worth adding") and "CLAIM" not in body

    def test_a_processed_claim_alone_is_worth_a_push(self):
        run = _run(loaded=_loaded(claim_outcomes=[self.OUTCOME]))
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title == "ffbot W2: free-agent check (wed 07:00)" and body.startswith("Your claim for")

    def test_nothing_to_add_is_an_all_clear_that_says_what_it_looked_at(self):
        avail = Availability(now=CLEARS, cycle_start=CLEARS, next_run=CLEARS + timedelta(days=7))
        run = _run(
            [_row("add", "Green Bay Packers", 0.9, week_gain=1.1, on_waivers=False)],
            loaded=_loaded(availability=avail, availability_source="sleeper"),
        )
        title, body = autorun.notification_for(run, _trigger("post_waiver"), _cfg(), {})
        assert title.endswith("-- nothing worth adding")
        assert "No claim of yours was processed at the run." in body
        assert (
            "Closest call: DEF Green Bay Packers for Detroit Lions, +1.1 pts this week "
            "(free-agent add worth +0.9, under your 2-point notify bar)"
        ) in body

    def test_an_unmodelled_run_never_claims_nothing_was_processed(self):
        _, body = autorun.notification_for(_run(), _trigger("post_waiver"), _cfg(), {})
        assert "No claim of yours" not in body and "Nothing worth a free-agent add." in body

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
        assert "Kaelon Black" in body
        assert "Not recommended" in body

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
        lines = autorun.speculative_lines(run, pre_run=False)
        assert any("3 rivals claimed him" in l for l in lines)

    def test_at_most_two_rows_reach_a_push(self):
        run = _run(waivers=[])
        run.speculative = [_spec(retrospective=False, name=f"Player {i}") for i in range(5)]
        lines = autorun.speculative_lines(run, pre_run=True)
        named = [l for l in lines if l.startswith("Also on the wire")]
        assert len(named) == 2

    def test_no_speculative_rows_adds_no_lines_at_all(self):
        run = _run(waivers=[])
        run.speculative = []
        assert autorun.speculative_lines(run, pre_run=True) == []
        assert autorun.speculative_lines(run, pre_run=False) == []
