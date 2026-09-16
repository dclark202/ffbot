from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ffbot import availability as av
from ffbot.live.schedule import eastern_to_utc, utc_to_eastern

SETTINGS = {"waiver_type": 0, "waiver_day_of_week": 2, "waiver_clear_days": 2, "daily_waivers": 0}

# This league's real week-1 transactions (2026): a Tuesday drop claimed off
# waivers Thursday 6:37pm ET, plus two plain free-agent add/drops.
DROP_4098 = {"type": "free_agent", "status": "complete", "created": 1788902481957,
             "status_updated": 1788902481957, "adds": {"12469": 2}, "drops": {"4098": 2}}
DROP_5892 = {"type": "free_agent", "status": "complete", "created": 1788910158824,
             "status_updated": 1788910158824, "adds": None, "drops": {"5892": 12}}
CLAIM_5892 = {"type": "waiver", "status": "complete", "created": 1788910250523,
              "status_updated": 1789079866429, "adds": {"5892": 12}, "drops": None, "settings": {"seq": 0}}


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _et(y, m, d, hh, mm=0):
    return eastern_to_utc(datetime(y, m, d, hh, mm))


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# A claim on a player nobody dropped: he was on waivers for having played, so
# it can only have processed at the weekly run -- Wed 2026-09-09 3:12am ET.
WEEKLY_CLAIM_7777 = {"type": "waiver", "status": "complete", "created": _ms(_et(2026, 9, 8, 21)),
                     "status_updated": _ms(_et(2026, 9, 9, 3, 12)), "adds": {"7777": 5}, "drops": None,
                     "roster_ids": [5], "settings": {"seq": 0}}

PLAYERS = {
    "4098": {"full_name": "Dropped Guy"},
    "5892": {"full_name": "Claimed Guy"},
    "12469": {"full_name": "Added Guy"},
    "7777": {"full_name": "Played Guy"},
    "DET": {"first_name": "Detroit", "last_name": "Lions"},
}

# Week 1 (Sep 13-14) and week 2 (Sep 17-20) kickoffs, US/Eastern, the shape
# `week.kickoffs_by_team` and `report.load_everything` hand over. GB is on
# bye in week 1 and opens week 2 on Thursday night.
WEEK1 = {"DET": "2026-09-13T13:00", "TEN": "2026-09-13T13:00", "KC": "2026-09-14T20:15"}
WEEK2 = {"DET": "2026-09-20T13:00", "TEN": "2026-09-20T13:00", "KC": "2026-09-20T16:25", "GB": "2026-09-17T20:15"}
RUN_W2 = _et(2026, 9, 16, 3, 5)  # the Wednesday run that ends week 1's waiver cycle


def _derive(transactions, now, kickoffs=None, players=PLAYERS, prior=None, **kw):
    return av.derive(SETTINGS, transactions, players, kickoffs or {}, now, prior_kickoffs_et=prior, **kw)


class TestWaivers:
    """The per-drop rule. Every `now` here is Wednesday 9/9 8am ET -- after
    that week's run, with no kickoffs -- so only a drop can put anyone on
    waivers."""

    def test_dropped_player_is_on_waivers_until_the_learned_run_on_his_clear_date(self):
        # Dropped Tue 9/8 5:21pm ET; clear date Thu 9/10; the league's one
        # processed claim on a dropped player ran at 6:37pm ET.
        a = _derive([DROP_4098, DROP_5892, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        status = a.status_for("Dropped Guy")
        assert status.status == av.WAIVERS
        assert status.clears_at == eastern_to_utc(datetime(2026, 9, 10, 18, 37))
        assert status.dropped_at is not None and "dropped Tue" in status.label()

    def test_the_real_week1_claim_matches_the_rule(self):
        # Player 5892, dropped Tue 7:09pm ET, was actually awarded at exactly
        # the time the rule predicts for a drop on that date.
        a = _derive([DROP_5892], now=_utc(2026, 9, 9, 12))
        processed = datetime.fromtimestamp(CLAIM_5892["status_updated"] / 1000, tz=timezone.utc)
        learned = _derive([DROP_5892, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        # re-added by the claim, so no longer on waivers at all...
        assert "claimed guy" not in learned.waivers
        # ...and without the claim, his clear date is the claim's own date.
        assert utc_to_eastern(a.status_for("Claimed Guy").clears_at).date() == utc_to_eastern(processed).date()

    def test_a_cleared_drop_is_a_free_agent(self):
        a = _derive([DROP_4098, DROP_5892, CLAIM_5892], now=_utc(2026, 9, 10, 23))
        assert a.status_for("Dropped Guy").status == av.FREE_AGENT

    def test_a_dropped_then_readded_player_is_not_on_waivers(self):
        a = _derive([DROP_5892, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        assert a.waivers == {}

    def test_never_dropped_and_not_played_since_the_run_is_a_free_agent(self):
        a = _derive([DROP_4098], now=_utc(2026, 9, 9, 12))
        assert a.status_for("Somebody Else").status == av.FREE_AGENT

    def test_no_processed_claim_keeps_him_on_waivers_through_the_end_of_his_clear_day(self):
        a = _derive([DROP_4098], now=_utc(2026, 9, 9, 12))
        assert a.status_for("Dropped Guy").clears_at == eastern_to_utc(datetime(2026, 9, 10, 23, 59))
        assert any("no processed waiver claim" in n for n in a.notes)

    def test_incomplete_transactions_are_ignored(self):
        failed = dict(DROP_4098, status="failed")
        a = _derive([failed], now=_utc(2026, 9, 9, 12))
        assert a.waivers == {}

    def test_defense_names_come_from_first_and_last(self):
        drop_def = dict(DROP_4098, drops={"DET": 2}, adds=None)
        a = _derive([drop_def, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        assert a.status_for("Detroit Lions").status == av.WAIVERS

    def test_unknown_player_id_is_a_note_not_a_crash(self):
        drop = dict(DROP_4098, drops={"999999": 2}, adds=None)
        a = _derive([drop], now=_utc(2026, 9, 9, 12))
        assert any("999999" in n for n in a.notes)


class TestWaiverCycle:
    """Sleeper: "if a player's game begins on Thursday night, they will lock
    at kickoff and remain on waivers until your selected waiver clear day."
    The rule the 2026-09-15 Tuesday check did not have -- it reported "0
    player(s) on waivers" and told the manager to add three defenses."""

    def test_before_his_kickoff_he_is_a_free_agent(self):
        a = _derive([], now=_et(2026, 9, 13, 12), kickoffs=WEEK1)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.FREE_AGENT and not s.game_started

    def test_from_kickoff_he_is_on_waivers_until_wednesdays_run(self):
        a = _derive([], now=_et(2026, 9, 13, 14), kickoffs=WEEK1)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.WAIVERS and s.game_started
        assert s.clears_at == RUN_W2 and s.played_at == _et(2026, 9, 13, 13)
        assert f"played {s.played_at.astimezone():%a}" in s.label() and av.local_clock(RUN_W2) in s.label()

    def test_after_his_game_he_is_still_on_waivers(self):
        a = _derive([], now=_et(2026, 9, 13, 17), kickoffs=WEEK1)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.WAIVERS and s.game_started and s.clears_at == RUN_W2

    def test_tuesday_evening_everyone_who_played_last_week_is_on_waivers(self):
        # The 2026-09-15 20:00 check: week 2 is current, its games are ahead,
        # and week 1's games put everyone who played on waivers.
        a = _derive([], now=_et(2026, 9, 15, 20), kickoffs=WEEK2, prior=WEEK1)
        for team in ("DET", "KC", "TEN"):
            s = a.status_for("Somebody", team)
            assert s.status == av.WAIVERS and s.clears_at == RUN_W2
            assert not s.game_started  # his WEEK-2 game is ahead: this week's points are intact
        assert a.status_for("Bye Guy", "GB").status == av.FREE_AGENT  # did not play last week
        assert a.played_teams() == ["DET", "KC", "TEN"]
        assert a.summary().startswith(f"Availability: on waivers until {av.local_clock(RUN_W2)} -- 3 team(s)")

    def test_wednesday_morning_free_agency_is_open(self):
        a = _derive([], now=_et(2026, 9, 16, 7), kickoffs=WEEK2, prior=WEEK1)
        assert a.status_for("Somebody", "DET").status == av.FREE_AGENT
        assert a.played_teams() == []
        assert f"free agency open since {av.local_clock(RUN_W2)}" in a.summary()

    def test_thursday_night_only_the_kicked_off_teams_are_back_on_waivers(self):
        a = _derive([], now=_et(2026, 9, 17, 21), kickoffs=WEEK2, prior=WEEK1)
        gb = a.status_for("Somebody", "GB")
        assert gb.status == av.WAIVERS and gb.game_started
        assert gb.clears_at == _et(2026, 9, 23, 3, 5)
        assert a.status_for("Somebody", "DET").status == av.FREE_AGENT

    def test_a_cleared_drop_who_played_stays_on_waivers_until_the_run(self):
        # Dropped Sun 9/13 9am: clear date Tue 9/15 at the learned 6:37pm. By
        # Tuesday night that has passed -- but he played Sunday.
        drop = dict(DROP_4098, created=_ms(_et(2026, 9, 13, 9)), status_updated=_ms(_et(2026, 9, 13, 9)))
        a = _derive([drop, DROP_5892, CLAIM_5892], now=_et(2026, 9, 15, 20), kickoffs=WEEK2, prior=WEEK1)
        s = a.status_for("Dropped Guy", "DET")
        assert s.status == av.WAIVERS and s.clears_at == RUN_W2 and s.dropped_at is None

    def test_a_drop_clearing_after_the_run_keeps_the_later_time(self):
        # Dropped Mon 9/14 8pm: clear date Wed 9/16 at the learned 6:37pm,
        # later than the 3:05am run -- Sleeper pushes the claim to the later one.
        drop = dict(DROP_4098, created=_ms(_et(2026, 9, 14, 20)), status_updated=_ms(_et(2026, 9, 14, 20)))
        a = _derive([drop, DROP_5892, CLAIM_5892], now=_et(2026, 9, 15, 20), kickoffs=WEEK2, prior=WEEK1)
        s = a.status_for("Dropped Guy", "DET")
        assert s.status == av.WAIVERS and s.clears_at == _et(2026, 9, 16, 18, 37)
        assert s.dropped_at is not None and s.played_at is not None

    def test_the_weekly_run_time_is_learned_only_from_a_claim_on_a_played_player(self):
        a = _derive([DROP_5892, CLAIM_5892, WEEKLY_CLAIM_7777], now=_et(2026, 9, 15, 20), kickoffs=WEEK2, prior=WEEK1)
        assert a.next_run == _et(2026, 9, 16, 3, 12)  # the Wednesday 3:12, not Thursday's 6:37
        assert a.status_for("Somebody", "DET").clears_at == a.next_run
        # ...and the Thursday claim still teaches the drop rule its 6:37pm.
        b = _derive([DROP_4098, DROP_5892, CLAIM_5892, WEEKLY_CLAIM_7777], now=_utc(2026, 9, 9, 12))
        assert b.status_for("Dropped Guy").clears_at == _et(2026, 9, 10, 18, 37)

    def test_a_learned_run_on_the_wrong_weekday_is_noted_not_used(self):
        # Sleeper counts Monday as 0. A run processed on a Wednesday under a
        # "Thursday" setting means the mapping (or the setting) is wrong.
        a = av.derive(dict(SETTINGS, waiver_day_of_week=3), [WEEKLY_CLAIM_7777], PLAYERS, WEEK2, _et(2026, 9, 15, 20))
        assert any("waiver_day_of_week" in n for n in a.notes)
        assert a.next_run == _et(2026, 9, 17, 3, 5)

    def test_daily_waivers_turn_the_weekly_rule_off_with_a_note(self):
        a = av.derive(dict(SETTINGS, daily_waivers=1), [], PLAYERS, WEEK1, _et(2026, 9, 13, 17))
        s = a.status_for("Cam Ward", "TEN")
        assert a.cycle_start is None and s.status == av.FREE_AGENT and s.game_started
        assert any("daily waivers" in n for n in a.notes)

    def test_no_run_day_in_the_settings_turns_the_rule_off_with_a_note(self):
        a = av.derive({"waiver_clear_days": 2}, [], PLAYERS, WEEK1, _et(2026, 9, 13, 17))
        assert a.cycle_start is None and a.status_for("Cam Ward", "TEN").status == av.FREE_AGENT
        assert any("waiver_day_of_week" in n for n in a.notes)

    def test_monday_is_weekday_zero(self):
        a = av.derive(dict(SETTINGS, waiver_day_of_week=0), [], PLAYERS, {}, _et(2026, 9, 15, 20))
        assert utc_to_eastern(a.next_run) == datetime(2026, 9, 21, 3, 5)

    def test_the_run_time_comes_from_config_until_learned(self):
        a = _derive([], now=_et(2026, 9, 15, 20), weekly_run_time_et="04:30")
        assert utc_to_eastern(a.next_run) == datetime(2026, 9, 16, 4, 30)
        bad = _derive([], now=_et(2026, 9, 15, 20), weekly_run_time_et="four-thirty")
        assert utc_to_eastern(bad.next_run) == datetime(2026, 9, 16, 3, 5)
        assert any("weekly_run_time_et" in n for n in bad.notes)

    def test_the_cycle_is_computed_in_eastern_across_a_daylight_time_change(self):
        a = _derive([], now=_et(2026, 10, 29, 12))
        assert a.next_run == eastern_to_utc(datetime(2026, 11, 4, 3, 5))  # EST
        assert a.cycle_start == eastern_to_utc(datetime(2026, 10, 28, 3, 5))  # EDT
        assert a.next_run - a.cycle_start == timedelta(days=7, hours=1)

    def test_a_defense_with_no_team_on_file_resolves_it_from_its_name(self):
        # The board's DST rows carry no team; on 2026-09-15 that made every
        # defense a free agent while the availability line said 32 teams
        # were on waivers.
        a = _derive([], now=_et(2026, 9, 15, 20), kickoffs=WEEK2, prior=WEEK1)
        assert a.status_for("Kansas City Chiefs", "").status == av.WAIVERS
        assert a.status_for("Kansas City Chiefs", "kc").clears_at == RUN_W2
        assert a.status_for("Green Bay Packers", "").status == av.FREE_AGENT  # bye last week
        sunday = _derive([], now=_et(2026, 9, 13, 14), kickoffs=WEEK1)
        assert sunday.status_for("Tennessee Titans", "").game_started

    def test_prior_kickoffs_never_feed_game_states(self):
        # `gameplan` locks every rostered player whose team is in
        # `game_states()`; last week's games must not do that on a Tuesday.
        a = av.Availability(
            now=_et(2026, 9, 15, 20), prior_kickoffs={"DET": _et(2026, 9, 13, 13)},
            cycle_start=_et(2026, 9, 9, 3, 5), next_run=RUN_W2,
        )
        assert a.status_for("Somebody", "DET").status == av.WAIVERS
        assert a.game_states() == {} and "in progress" not in a.summary()

    def test_a_hand_built_availability_has_the_rule_off(self):
        a = av.Availability(now=_et(2026, 9, 15, 20), kickoffs={"TEN": _et(2026, 9, 13, 13)})
        assert a.status_for("Cam Ward", "TEN").status == av.FREE_AGENT
        assert a.summary() == "Availability: 0 player(s) on waivers; every other unrostered player is a free agent"

    def test_no_kickoffs_is_a_note(self):
        a = _derive([], now=_utc(2026, 9, 13, 18))
        assert any("kickoff times unknown" in n for n in a.notes)


class TestAcquisitionVerdict:
    SCALE = 12.0

    def _cfg(self, priority_value=0.3):
        from ffbot.config import Config, SeasonConfig

        return Config(season=SeasonConfig(priority_value=priority_value))

    def test_a_free_agent_is_never_charged_priority(self):
        from ffbot import week

        for priority in (1, 6, 12, None):
            cost, note, kind = week.acquisition_verdict(
                av.PlayerAvailability(), 0.1, priority, 12, self._cfg(priority_value=50.0), scale=self.SCALE,
            )
            assert (cost, kind) == (0.0, "add")
            assert note.startswith("FREE AGENT")

    def test_a_waiver_player_keeps_the_claim_economics(self):
        from ffbot import week

        cfg = self._cfg()
        avail = av.PlayerAvailability(status=av.WAIVERS, clears_at=_utc(2026, 9, 10, 22, 37))
        big = week.acquisition_verdict(avail, 54.4, 3, 12, cfg, scale=self.SCALE)
        assert big[2] == "claim"
        assert big[0] == week.claim_verdict(54.4, 3, 12, cfg, scale=self.SCALE)[0]
        small = week.acquisition_verdict(avail, 0.1, 1, 12, cfg, scale=self.SCALE)
        assert small[0] == 0.0 and small[2] == "wait"
        assert "on waivers" in small[1]

    def test_a_played_waiver_player_goes_through_the_same_economics(self):
        # His game has kicked off: on waivers until the run, claimable now.
        from ffbot import week

        cfg = self._cfg()
        avail = av.PlayerAvailability(
            status=av.WAIVERS, clears_at=RUN_W2, played_at=_et(2026, 9, 13, 13), game_started=True,
        )
        cost, note, kind = week.acquisition_verdict(avail, 30.0, 1, 12, cfg, scale=self.SCALE)
        assert kind == "claim" and cost == week.claim_verdict(30.0, 1, 12, cfg, scale=self.SCALE)[0]
        assert "on waivers" in note

    def test_unknown_status_is_priced_exactly_as_a_claim(self):
        from ffbot import week

        cfg = self._cfg()
        for gain in (0.1, 54.4):
            cost, note, is_claim = week.claim_verdict(gain, 5, 12, cfg, scale=self.SCALE)
            assert week.acquisition_verdict(None, gain, 5, 12, cfg, scale=self.SCALE) == (
                cost, note, "claim" if is_claim else "wait",
            )


class TestClaimOutcomes:
    """What Wednesday's check says about Tuesday's claims."""

    SINCE = _et(2026, 9, 16, 2, 5)
    WON = dict(WEEKLY_CLAIM_7777, status_updated=_ms(RUN_W2), adds={"7777": 5}, drops={"4098": 5}, roster_ids=[5])

    def test_my_processed_claim_is_reported(self):
        outs = av.claim_outcomes([self.WON], 5, self.SINCE, PLAYERS)
        assert [o.status for o in outs] == ["complete"]
        assert outs[0].text() == "Your claim for Played Guy (dropping Dropped Guy): processed"

    def test_a_failed_claim_carries_sleepers_reason(self):
        lost = dict(self.WON, status="failed", metadata={"notes": "Another owner had higher priority."})
        outs = av.claim_outcomes([lost], 5, self.SINCE, PLAYERS)
        assert outs[0].text() == "Your claim for Played Guy (dropping Dropped Guy): failed -- Another owner had higher priority."

    def test_other_rosters_older_claims_and_free_agent_moves_are_ignored(self):
        theirs = dict(self.WON, roster_ids=[3])
        old = dict(self.WON, status_updated=_ms(_et(2026, 9, 10, 18, 37)))
        fa = dict(self.WON, type="free_agent")
        assert av.claim_outcomes([theirs, old, fa], 5, self.SINCE, PLAYERS) == []

    def test_the_availability_asks_about_its_own_cycles_run(self):
        a = av.Availability(now=RUN_W2 + timedelta(hours=4), cycle_start=RUN_W2, next_run=RUN_W2 + timedelta(days=7))
        assert [o.status for o in a.claim_outcomes_since_run([self.WON], 5, PLAYERS)] == ["complete"]
        off = av.Availability(now=RUN_W2 + timedelta(hours=4))
        assert off.claim_outcomes_since_run([self.WON], 5, PLAYERS) == []

    def test_oldest_first(self):
        later = dict(self.WON, status_updated=_ms(RUN_W2 + timedelta(minutes=1)), adds={"5892": 5}, drops=None)
        outs = av.claim_outcomes([later, self.WON], 5, self.SINCE, PLAYERS)
        assert [o.add_name for o in outs] == ["Played Guy", "Claimed Guy"]


class TestEasternRoundTrip:
    def test_daylight_and_standard_time(self):
        for naive in (datetime(2026, 9, 13, 13, 0), datetime(2026, 12, 20, 20, 15)):
            assert utc_to_eastern(eastern_to_utc(naive)) == naive
