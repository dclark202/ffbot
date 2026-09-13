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

PLAYERS = {
    "4098": {"full_name": "Dropped Guy"},
    "5892": {"full_name": "Claimed Guy"},
    "12469": {"full_name": "Added Guy"},
    "DET": {"first_name": "Detroit", "last_name": "Lions"},
}


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _derive(transactions, now, kickoffs=None, players=PLAYERS):
    return av.derive(SETTINGS, transactions, players, kickoffs or {}, now)


class TestWaivers:
    def test_dropped_player_is_on_waivers_until_the_learned_run_on_his_clear_date(self):
        # Dropped Tue 9/8 5:21pm ET; clear date Thu 9/10; the league's one
        # processed claim ran at 6:37pm ET.
        a = _derive([DROP_4098, DROP_5892, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        status = a.status_for("Dropped Guy")
        assert status.status == av.WAIVERS
        assert status.clears_at == eastern_to_utc(datetime(2026, 9, 10, 18, 37))

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
        a = _derive([DROP_4098, CLAIM_5892], now=_utc(2026, 9, 10, 23))
        assert a.status_for("Dropped Guy").status == av.FREE_AGENT

    def test_a_dropped_then_readded_player_is_not_on_waivers(self):
        a = _derive([DROP_5892, CLAIM_5892], now=_utc(2026, 9, 9, 12))
        assert a.waivers == {}

    def test_never_dropped_is_a_free_agent(self):
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


class TestGameLock:
    KICKOFFS = {"TEN": "2026-09-13T13:00"}

    def test_before_kickoff_is_a_free_agent_with_his_week_ahead(self):
        a = _derive([], now=eastern_to_utc(datetime(2026, 9, 13, 12, 0)), kickoffs=self.KICKOFFS)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.FREE_AGENT and not s.game_started

    def test_mid_game_is_locked_until_the_game_ends(self):
        a = _derive([], now=eastern_to_utc(datetime(2026, 9, 13, 14, 0)), kickoffs=self.KICKOFFS)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.LOCKED and s.game_started
        assert s.unlocks_at == eastern_to_utc(datetime(2026, 9, 13, 13, 0)) + timedelta(hours=3.5)

    def test_after_the_game_he_is_a_free_agent_whose_week_is_over(self):
        a = _derive([], now=eastern_to_utc(datetime(2026, 9, 13, 17, 0)), kickoffs=self.KICKOFFS)
        s = a.status_for("Cam Ward", "TEN")
        assert s.status == av.FREE_AGENT and s.game_started

    def test_waivers_outrank_the_lock(self):
        drop = dict(DROP_4098, drops={"4098": 2}, created=1789300000000, status_updated=1789300000000)
        now = eastern_to_utc(datetime(2026, 9, 13, 14, 0))
        a = av.derive(SETTINGS, [drop, CLAIM_5892], PLAYERS, self.KICKOFFS, now)
        s = a.status_for("Dropped Guy", "TEN")
        assert s.status == av.WAIVERS and s.game_started

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

    def test_locked_waits_and_costs_nothing(self):
        from ffbot import week

        avail = av.PlayerAvailability(status=av.LOCKED, unlocks_at=_utc(2026, 9, 13, 20), game_started=True)
        cost, note, kind = week.acquisition_verdict(avail, 30.0, 1, 12, self._cfg(), scale=self.SCALE)
        assert (cost, kind) == (0.0, "wait") and note.startswith("LOCKED")

    def test_unknown_status_is_priced_exactly_as_a_claim(self):
        from ffbot import week

        cfg = self._cfg()
        for gain in (0.1, 54.4):
            cost, note, is_claim = week.claim_verdict(gain, 5, 12, cfg, scale=self.SCALE)
            assert week.acquisition_verdict(None, gain, 5, 12, cfg, scale=self.SCALE) == (
                cost, note, "claim" if is_claim else "wait",
            )


class TestEasternRoundTrip:
    def test_daylight_and_standard_time(self):
        for naive in (datetime(2026, 9, 13, 13, 0), datetime(2026, 12, 20, 20, 15)):
            assert utc_to_eastern(eastern_to_utc(naive)) == naive
