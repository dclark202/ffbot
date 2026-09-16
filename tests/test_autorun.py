from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ffbot.live.schedule import LiveGame
from scripts import autorun
from scripts import week_report as wr_module


def _game(opponent, home, kickoff):
    return LiveGame(opponent=opponent, home=home, roof="outdoors", kickoff=kickoff)


def _stub_run(week_num=1, moves=None, waivers=None, sections=None):
    """A minimal `week_report.ReportRun` stand-in -- enough for `_fire`'s
    own rendering (`sections`) and `actionable_summary` (`brief.lineup.
    moves`, `waivers`) without a real `load_everything` call."""
    brief = SimpleNamespace(lineup=SimpleNamespace(moves=moves or []))
    return wr_module.ReportRun(
        week=week_num, loaded=None, brief=brief,
        waivers=waivers or [], sections=sections or [f"WEEK {week_num}"],
    )


def _waiver_candidate(add_name="Someone", net=5.0, claim=True, drop_name="Bench Guy"):
    return SimpleNamespace(
        add_name=add_name, net=net, drop_name=drop_name,
        kind="claim" if claim else "add",
        claim_note=(
            "CLAIM (priority 3/12)" if claim
            else "HOLD PRIORITY -- +0.6 is under the 1.4 a priority-3/12 slot is worth"
        ),
    )


class TestNotifyThresholdIsNotZero:
    """`notify.min_waiver_net` shipped at 0.0 through week 1 of 2026, which
    defeated its own documented intent ("not buzz a phone for a marginal
    one") -- combined with every positive-gain row being typed CLAIM, a
    +0.6-point DEF sidegrade pushed to a phone on 2026-09-09."""

    def test_shipped_config_does_not_notify_on_a_marginal_claim(self):
        from ffbot.config import Config

        cfg = Config.load("config.yml")
        assert cfg.notify.min_waiver_net > 0.0
        run = _stub_run(waivers=[_waiver_candidate(net=0.6, claim=True)])
        assert autorun.actionable_summary(run, cfg.notify.min_waiver_net) == []

    def test_shipped_config_still_notifies_on_a_real_claim(self):
        from ffbot.config import Config

        cfg = Config.load("config.yml")
        run = _stub_run(waivers=[_waiver_candidate(add_name="David Montgomery", net=55.4, claim=True)])
        lines = autorun.actionable_summary(run, cfg.notify.min_waiver_net)
        assert any("David Montgomery" in ln for ln in lines)

    def test_dataclass_default_matches_the_shipped_intent(self):
        from ffbot.config import NotifyConfig

        assert NotifyConfig().min_waiver_net > 0.0


class TestFreeAgentAddsNotify:
    def _row(self, kind, net=5.0, status="free_agent"):
        from ffbot.availability import PlayerAvailability

        return SimpleNamespace(
            add_name="Malik Willis", net=net, drop_name="Tyjae Spears", kind=kind,
            claim_note="FREE AGENT -- add now, no waiver claim",
            availability=PlayerAvailability(status=status),
        )

    def test_a_free_agent_add_over_the_bar_notifies_as_a_free_agent(self):
        lines = autorun.actionable_summary(_stub_run(waivers=[self._row("add")]), 2.0)
        # Sectioned and verb-led now -- one instruction, not a sentence.
        assert lines == ["\n".join(["ADD/DROP", "  ADD Malik Willis  DROP Tyjae Spears"])]

    def test_under_the_bar_stays_quiet(self):
        assert autorun.actionable_summary(_stub_run(waivers=[self._row("add", net=1.0)]), 2.0) == []

    def test_a_wait_row_never_notifies(self):
        assert autorun.actionable_summary(_stub_run(waivers=[self._row("wait", status="waivers")]), 2.0) == []

    def test_unknown_status_adds_do_not_notify(self):
        row = self._row("add")
        row.availability = None
        assert autorun.actionable_summary(_stub_run(waivers=[row]), 2.0) == []


class TestThisCalendarWeekAt:
    def test_same_weekday_keeps_the_date(self):
        # Tuesday 2026-08-11 -> Tuesday of the same week, different hour.
        now = datetime(2026, 8, 11, 9, 0)
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)  # tue
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_later_in_the_week_looks_back_to_this_weeks_occurrence(self):
        # Friday -> the Tuesday earlier in the SAME calendar week, not next week's.
        now = datetime(2026, 8, 14, 9, 0)  # Friday
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_earlier_in_the_week_resolves_forward_to_this_weeks_occurrence(self):
        # Monday -> the Tuesday still to come THIS week (tomorrow), never
        # last week's Tuesday -- the bug this function's docstring calls
        # out explicitly: a naive "most recent past occurrence" calculation
        # would jump back a full 6 days here instead of forward 1.
        now = datetime(2026, 8, 10, 9, 0)  # Monday
        out = autorun._this_calendar_week_at(now, weekday=1, hour=20)
        assert out == datetime(2026, 8, 11, 20, 0)

    def test_never_returns_a_date_before_this_weeks_monday(self):
        # A direct regression guard for the backward-jump bug: for every
        # (now weekday, target weekday) pair, the result must fall within
        # [this Monday, this Monday + 6 days].
        for now_wd in range(7):
            monday = datetime(2026, 8, 10) + timedelta(days=now_wd)  # 2026-08-10 is a Monday
            for target_wd in range(7):
                out = autorun._this_calendar_week_at(monday, weekday=target_wd, hour=12)
                this_monday = datetime(2026, 8, 10)
                assert this_monday <= out < this_monday + timedelta(days=7)


class TestBuildTriggers:
    def test_one_trigger_per_distinct_kickoff_not_per_game(self):
        k1 = datetime(2026, 9, 13, 13, 0)
        k2 = datetime(2026, 9, 13, 13, 0)  # same slot, different game
        k3 = datetime(2026, 9, 13, 20, 20)
        games = {
            "A": _game("B", True, k1), "B": _game("A", False, k1),
            "C": _game("D", True, k2), "D": _game("C", False, k2),
            "E": _game("F", True, k3), "F": _game("E", False, k3),
        }
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        kickoff_triggers = [t for t in triggers if t.id.startswith("pre_kickoff_")]
        assert len(kickoff_triggers) == 2  # k1 and k3, deduplicated

    def test_kickoff_trigger_due_at_is_lead_minutes_before_kickoff(self):
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        kickoff_trigger = next(t for t in triggers if t.id.startswith("pre_kickoff_"))
        assert kickoff_trigger.due_at == kickoff - timedelta(minutes=120)

    def test_includes_exactly_one_pre_waiver_trigger(self):
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers({}, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        waiver_triggers = [t for t in triggers if t.id.startswith("pre_waiver_")]
        assert len(waiver_triggers) == 1

    def test_no_kickoff_time_is_skipped_not_a_crash(self):
        games = {"A": _game("B", True, None), "B": _game("A", False, None)}
        now = datetime(2026, 9, 13, 8, 0)
        triggers = autorun.build_triggers(games, now, lead_minutes=120, waiver_weekday="tue", waiver_hour=20)
        assert not any(t.id.startswith("pre_kickoff_") for t in triggers)

    def test_trigger_ids_stable_across_calls(self):
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        now1 = datetime(2026, 9, 13, 8, 0)
        now2 = datetime(2026, 9, 13, 9, 0)  # a later poll, same week
        t1 = autorun.build_triggers(games, now1, 120, "tue", 20)
        t2 = autorun.build_triggers(games, now2, 120, "tue", 20)
        ids1 = {t.id for t in t1}
        ids2 = {t.id for t in t2}
        assert ids1 == ids2


class TestIsDue:
    def test_not_due_before_due_at(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 11, 59), fired=set()) is False

    def test_due_at_due_at(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 0), fired=set()) is True

    def test_due_within_grace_period(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 20), fired=set()) is True

    def test_not_due_past_grace_period(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 31), fired=set()) is False

    def test_not_due_if_already_fired(self):
        t = autorun.Trigger(id="x", due_at=datetime(2026, 1, 1, 12, 0), grace_minutes=30, label="")
        assert autorun._is_due(t, datetime(2026, 1, 1, 12, 5), fired={"x"}) is False


class TestStatePersistence:
    def test_missing_state_file_returns_empty_dict(self, tmp_path):
        assert autorun._load_state(tmp_path / "nope.json") == {}

    def test_corrupt_state_file_degrades_to_empty_dict(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        assert autorun._load_state(path) == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "sub" / "state.json"
        autorun._save_state(path, {"2026-w01": ["pre_waiver_2026-08-11"]})
        assert autorun._load_state(path) == {"2026-w01": ["pre_waiver_2026-08-11"]}


class TestMainDryRun:
    def test_dry_run_prints_schedule_and_writes_no_state(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)

        rc = autorun.main(["--dry-run", "--season", "2026"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "week 1" in out
        assert "pre_kickoff_" in out
        assert "pre_waiver_" in out
        assert "notify: channel='off'" in out  # default config.yml has no notify: block
        assert not (tmp_path / "data" / "autorun_state.json").exists()

    def test_schedule_failure_returns_nonzero_never_raises(self, tmp_path, monkeypatch, capsys):
        from ffbot.live.schedule import ScheduleError

        monkeypatch.chdir(tmp_path)

        def raising(season):
            raise ScheduleError("simulated network failure")

        monkeypatch.setattr(autorun, "current_week", raising)
        rc = autorun.main(["--season", "2026"])
        assert rc == 1
        assert "schedule fetch failed" in capsys.readouterr().err

    def test_chdir_switches_working_directory_before_config_load(self, tmp_path, monkeypatch, capsys):
        # scripts/schedule_autorun.py always passes --chdir (a scheduled
        # task has no "start in" the way a terminal does) -- this proves
        # config.yml/data/autorun_state.json resolve against --chdir's
        # directory, not whatever cwd the task scheduler happened to use.
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)

        rc = autorun.main(["--chdir", str(real_dir), "--dry-run", "--season", "2026"])
        assert rc == 0
        assert Path.cwd() == real_dir
        assert not (elsewhere / "data" / "autorun_state.json").exists()


class TestMainFiring:
    def test_nothing_due_touches_no_state_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        far_future_kickoff = datetime(2099, 1, 1, 20, 20)
        games = {"A": _game("B", True, far_future_kickoff), "B": _game("A", False, far_future_kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)

        rc = autorun.main(["--season", "2026", "--waiver-weekday", "tue", "--waiver-hour", "20"])
        assert rc == 0
        # nothing was due (kickoff far in the future, and "now" in this
        # test is real "now" which may or may not be past the configured
        # waiver slot -- but even if it were due, firing it would touch
        # the network; assert conservatively that if it WAS fired the
        # state file exists and is well-formed, otherwise it's absent)
        state_path = tmp_path / "data" / "autorun_state.json"
        if state_path.exists():
            json.loads(state_path.read_text())  # must still be valid JSON

    def test_due_trigger_fires_and_records_state(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        # Freeze "now" to exactly the kickoff trigger's due_at.
        # `main` converts the schedule's US-Eastern kickoff to this machine's
        # clock; pinned to identity so the frozen `now` below lines up with the
        # trigger's due time in every timezone, not just Central.
        monkeypatch.setattr(autorun, "eastern_to_local", lambda k: k)
        due_at = kickoff - timedelta(minutes=autorun.parse_args([]).lead_minutes)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        calls = []

        def fake_run_report(args):
            calls.append(args)
            return _stub_run(week_num=args.week)

        monkeypatch.setattr(autorun.week_report, "run_report", fake_run_report)

        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert len(calls) == 1
        assert calls[0].no_save_state is True
        assert calls[0].refresh is True

        state = json.loads((tmp_path / "data" / "autorun_state.json").read_text())
        fired = state["2026-w01"]
        assert any(f.startswith("pre_kickoff_") for f in fired)
        # The rendered report was written to reports/, using run_report's
        # own sections rather than week_report.main()'s stdout/--out path.
        report_files = list((tmp_path / "reports").glob("*.md"))
        assert len(report_files) == 1
        assert "WEEK 1" in report_files[0].read_text(encoding="utf-8")

        # A fired check also asks week_report for a structured record of what
        # it recommended, labelled with THIS trigger. Autorun used to keep
        # only the rendered markdown, so nothing survived about why a
        # scheduled run said what it said. The suffix is the sanitized one
        # (a pre-kickoff trigger id embeds an ISO timestamp whose colons are
        # not legal in a Windows filename), matching reports/'s own naming.
        assert calls[0].week_log_source
        assert ":" not in calls[0].week_log_source
        assert calls[0].week_log_source in report_files[0].name

    def test_failed_fire_does_not_mark_trigger_fired(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        # `main` converts the schedule's US-Eastern kickoff to this machine's
        # clock; pinned to identity so the frozen `now` below lines up with the
        # trigger's due time in every timezone, not just Central.
        monkeypatch.setattr(autorun, "eastern_to_local", lambda k: k)
        due_at = kickoff - timedelta(minutes=autorun.parse_args([]).lead_minutes)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        def raising_run_report(args):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(autorun.week_report, "run_report", raising_run_report)

        rc = autorun.main(["--season", "2026"])
        assert rc == 0  # a single failed trigger does not crash the whole poll
        state_path = tmp_path / "data" / "autorun_state.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            assert state.get("2026-w01", []) == []


class TestActionableSummary:
    def test_lineup_moves_are_always_included(self):
        run = _stub_run(moves=["Josh Allen: BN -> QB (proj 22.0)"])
        summary = autorun.actionable_summary(run, min_waiver_net=0.0)
        body = "\n".join(summary)
        assert "START/SIT" in body
        assert "Josh Allen: BN -> QB (proj 22.0)" in body

    def test_moves_are_capped_so_the_push_stays_a_glance(self):
        run = _stub_run(moves=[f"Move {i}" for i in range(8)])
        body = "\n".join(autorun.actionable_summary(run, min_waiver_net=0.0))
        shown = [l for l in body.split("\n") if l.strip().startswith("Move ")]
        assert len(shown) == 4
        assert "+4 more" in body

    def test_claim_over_threshold_is_included(self):
        run = _stub_run(waivers=[_waiver_candidate(net=5.0, claim=True)])
        summary = autorun.actionable_summary(run, min_waiver_net=2.0)
        assert any("CLAIM Someone" in line for line in summary)

    def test_claim_under_threshold_is_suppressed(self):
        run = _stub_run(waivers=[_waiver_candidate(net=1.0, claim=True)])
        summary = autorun.actionable_summary(run, min_waiver_net=2.0)
        assert summary == []

    def test_hold_priority_candidate_never_notifies_regardless_of_net(self):
        run = _stub_run(waivers=[_waiver_candidate(net=50.0, claim=False)])
        summary = autorun.actionable_summary(run, min_waiver_net=0.0)
        assert summary == []

    def test_noop_run_is_empty(self):
        run = _stub_run(moves=[], waivers=[])
        assert autorun.actionable_summary(run, min_waiver_net=0.0) == []

    def test_moves_and_claims_combine(self):
        run = _stub_run(moves=["A move"], waivers=[_waiver_candidate(net=5.0, claim=True)])
        summary = autorun.actionable_summary(run, min_waiver_net=0.0)
        assert any("move" in line.lower() for line in summary)
        assert any("CLAIM" in line for line in summary)


class TestMainNotifications:
    def _fire_setup(self, tmp_path, monkeypatch, extra_config_yaml="", run_kwargs=None):
        monkeypatch.chdir(tmp_path)
        kickoff = datetime(2026, 9, 13, 20, 20)
        games = {"A": _game("B", True, kickoff), "B": _game("A", False, kickoff)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        # `main` converts the schedule's US-Eastern kickoff to this machine's
        # clock; pinned to identity so the frozen `now` below lines up with the
        # trigger's due time in every timezone, not just Central.
        monkeypatch.setattr(autorun, "eastern_to_local", lambda k: k)
        due_at = kickoff - timedelta(minutes=autorun.parse_args([]).lead_minutes)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)
        monkeypatch.setattr(
            autorun.week_report, "run_report",
            lambda args: _stub_run(week_num=args.week, **(run_kwargs or {})),
        )
        if extra_config_yaml:
            (tmp_path / "config.yml").write_text(extra_config_yaml, encoding="utf-8")

    def test_notify_called_when_actionable_and_channel_on(self, tmp_path, monkeypatch):
        self._fire_setup(
            tmp_path, monkeypatch,
            extra_config_yaml="notify:\n  channel: ntfy\n  ntfy_topic: test-topic\n",
            run_kwargs={"moves": ["A move"]},
        )
        calls = []
        monkeypatch.setattr(
            "ffbot.notify.send", lambda cfg, title, body, **kw: calls.append((title, body)) or [],
        )
        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert len(calls) == 1
        assert "A move" in calls[0][1]

    def test_notify_not_called_when_channel_off(self, tmp_path, monkeypatch):
        self._fire_setup(tmp_path, monkeypatch, run_kwargs={"moves": ["A move"]})
        calls = []
        monkeypatch.setattr("ffbot.notify.send", lambda cfg, title, body, **kw: calls.append(1) or [])
        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert calls == []

    def test_quiet_pre_kickoff_check_sends_an_all_clear(self, tmp_path, monkeypatch):
        """Formerly test_notify_not_called_when_noop. A quiet pre-kickoff check
        used to send nothing, indistinguishable from a check that never ran."""
        self._fire_setup(
            tmp_path, monkeypatch,
            extra_config_yaml="notify:\n  channel: ntfy\n  ntfy_topic: test-topic\n",
            run_kwargs={"moves": [], "waivers": []},
        )
        calls = []
        monkeypatch.setattr("ffbot.notify.send", lambda cfg, title, body, **kw: calls.append((title, body)) or [])
        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert len(calls) == 1
        assert "all clear" in calls[0][0]

    def test_notify_not_called_when_noop_and_heartbeat_off(self, tmp_path, monkeypatch):
        self._fire_setup(
            tmp_path, monkeypatch,
            extra_config_yaml="notify:\n  channel: ntfy\n  ntfy_topic: test-topic\n  heartbeat: false\n",
            run_kwargs={"moves": [], "waivers": []},
        )
        calls = []
        monkeypatch.setattr("ffbot.notify.send", lambda cfg, title, body, **kw: calls.append(1) or [])
        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert calls == []

    def test_notify_failure_alert_printed_but_trigger_still_marked_fired(self, tmp_path, monkeypatch, capsys):
        self._fire_setup(
            tmp_path, monkeypatch,
            extra_config_yaml="notify:\n  channel: ntfy\n  ntfy_topic: test-topic\n",
            run_kwargs={"moves": ["A move"]},
        )
        monkeypatch.setattr("ffbot.notify.send", lambda cfg, title, body, **kw: ["ntfy: delivery failed (simulated)"])
        rc = autorun.main(["--season", "2026"])
        assert rc == 0
        assert "ntfy: delivery failed" in capsys.readouterr().err
        state = json.loads((tmp_path / "data" / "autorun_state.json").read_text())
        assert any(f.startswith("pre_kickoff_") for f in state["2026-w01"])


class TestEasternToLocal:
    """nflverse kickoffs are naive US-Eastern; `main` compares against the
    machine's local clock. Converting was missing entirely, so a "2h" lead
    ran 1h out on a Central-time machine (and would fire AT kickoff in
    Pacific)."""

    def test_september_kickoff_is_daylight_time(self):
        assert autorun.eastern_to_utc(datetime(2026, 9, 10, 20, 35)) == datetime(
            2026, 9, 11, 0, 35, tzinfo=timezone.utc
        )

    def test_early_international_kickoff_in_october_is_daylight_time(self):
        assert autorun.eastern_to_utc(datetime(2026, 10, 4, 9, 30)) == datetime(
            2026, 10, 4, 13, 30, tzinfo=timezone.utc
        )

    def test_after_the_november_change_is_standard_time(self):
        assert autorun.eastern_to_utc(datetime(2026, 11, 1, 13, 0)) == datetime(
            2026, 11, 1, 18, 0, tzinfo=timezone.utc
        )

    def test_january_playoffs_are_standard_time(self):
        assert autorun.eastern_to_utc(datetime(2027, 1, 17, 16, 30)) == datetime(
            2027, 1, 17, 21, 30, tzinfo=timezone.utc
        )

    def test_changeover_boundaries(self):
        # 2026: daylight time starts Sun Mar 8 02:00, ends Sun Nov 1 02:00.
        assert not autorun._eastern_is_dst(datetime(2026, 3, 8, 1, 59))
        assert autorun._eastern_is_dst(datetime(2026, 3, 8, 2, 0))
        assert autorun._eastern_is_dst(datetime(2026, 11, 1, 1, 59))
        assert not autorun._eastern_is_dst(datetime(2026, 11, 1, 2, 0))

    def test_needs_no_named_zone_lookup(self):
        """Windows ships no IANA database and this repo adds no tzdata
        dependency -- a named-zone lookup would crash the scheduled task on
        the one platform it runs on."""
        import inspect

        src = inspect.getsource(autorun)
        assert "ZoneInfo(" not in src and "import zoneinfo" not in src and "from zoneinfo" not in src


class TestTriggersUseLocalKickoffTime:
    KICKOFF_ET = datetime(2026, 9, 10, 20, 35)

    def _trigger(self, to_local):
        games = {"LAR": _game("SF", False, self.KICKOFF_ET), "SF": _game("LAR", True, self.KICKOFF_ET)}
        triggers = autorun.build_triggers(
            games, datetime(2026, 9, 10, 8, 0), lead_minutes=60,
            waiver_weekday="tue", waiver_hour=20, to_local=to_local,
        )
        return next(t for t in triggers if t.kickoff is not None)

    def test_due_at_is_computed_from_the_local_kickoff(self):
        central = lambda k: k - timedelta(hours=1)  # noqa: E731
        assert self._trigger(central).due_at == datetime(2026, 9, 10, 18, 35)

    def test_trigger_id_stays_on_the_schedules_eastern_time(self):
        """Idempotency. data/autorun_state.json already holds ids like
        pre_kickoff_2026-09-10T20:35:00 -- keying on local time instead would
        re-fire checks that already ran."""
        central = lambda k: k - timedelta(hours=1)  # noqa: E731
        t = self._trigger(central)
        assert t.id == "pre_kickoff_2026-09-10T20:35:00"
        assert t.kickoff == self.KICKOFF_ET
        assert t.local_kickoff == datetime(2026, 9, 10, 19, 35)

    def test_label_reads_in_local_time(self):
        central = lambda k: k - timedelta(hours=1)  # noqa: E731
        assert "thu 19:35" in self._trigger(central).label

    def test_main_wires_the_eastern_conversion(self):
        import inspect

        assert "to_local=eastern_to_local" in inspect.getsource(autorun.main)

    def test_default_lead_lands_after_nfl_inactives(self):
        """Inactives are due 90 minutes before kickoff; a check further out
        than that cannot see a surprise scratch."""
        assert autorun.parse_args([]).lead_minutes < 90


def _player(name, team, proj):
    return SimpleNamespace(name=name, team=team, projected_points=proj)


class TestHeartbeat:
    """The pre-kickoff all-clear. On 2026-09-10 the 18:47 check ran, found
    nothing, and sent nothing -- indistinguishable from a dead task forty
    minutes before kickoff. A quiet pre-kickoff check now says so."""

    EARLY = datetime(2026, 9, 13, 13, 0)
    LATE = datetime(2026, 9, 13, 16, 25)

    def _trigger(self, kickoff=EARLY):
        return autorun.Trigger(
            id=f"pre_kickoff_{kickoff.isoformat()}", due_at=kickoff - timedelta(hours=2),
            grace_minutes=90, label="pre-kickoff check", kickoff=kickoff,
            local_kickoff=kickoff - timedelta(hours=1),
        )

    def _waiver_trigger(self):
        return autorun.Trigger(id="pre_waiver_2026-09-15", due_at=datetime(2026, 9, 15, 20, 0),
                               grace_minutes=720, label="check (tue 20:00)", kind="waiver")

    def _run(self, *, waivers=None, starters=None, loaded=None):
        brief = SimpleNamespace(lineup=SimpleNamespace(assignments=starters or [], moves=[]))
        return wr_module.ReportRun(week=2, loaded=loaded, brief=brief, waivers=waivers or [], sections=["WEEK 2"])

    def _cfg(self, heartbeat=True):
        return SimpleNamespace(notify=SimpleNamespace(min_waiver_net=2.0, heartbeat=heartbeat))

    def _games(self):
        return {
            "SEA": _game("NE", True, self.EARLY), "NE": _game("SEA", False, self.EARLY),
            "BAL": _game("IND", False, self.LATE), "IND": _game("BAL", True, self.LATE),
        }

    def test_a_quiet_pre_kickoff_check_sends_an_all_clear(self):
        msg = autorun.notification_for(self._run(), self._trigger(), self._cfg(), self._games())
        assert msg is not None
        title, body = msg
        assert "all clear" in title
        assert "Sun 12:00" in title  # local kickoff, not the schedule's Eastern time
        assert "No lineup changes" in body

    def test_an_actionable_check_sends_the_action_not_an_all_clear(self):
        run = self._run(waivers=[_waiver_candidate(add_name="David Montgomery", net=55.4, claim=True)])
        title, body = autorun.notification_for(run, self._trigger(), self._cfg(), self._games())
        assert "all clear" not in title
        assert "CLAIM David Montgomery" in body

    def test_a_quiet_waiver_check_says_nothing_worth_a_claim(self):
        # The manager's call (2026-09-15): an explicit "no claim" beats
        # silence on the one night the check exists for.
        title, body = autorun.notification_for(self._run(), self._waiver_trigger(), self._cfg(), self._games())
        assert title.endswith("nothing worth a claim")
        assert body.startswith("No claim worth your priority tonight.")

    def test_a_quiet_waiver_check_with_heartbeat_off_stays_quiet(self):
        assert autorun.notification_for(
            self._run(), self._waiver_trigger(), self._cfg(heartbeat=False), self._games(),
        ) is None

    def test_heartbeat_off_restores_the_old_silence(self):
        assert autorun.notification_for(self._run(), self._trigger(), self._cfg(heartbeat=False), self._games()) is None

    def test_names_only_the_starters_locking_in_this_slot(self):
        starters = [("WR", _player("Jaxon Smith-Njigba", "SEA", 19.2)), ("RB", _player("Derrick Henry", "BAL", 16.1))]
        _, body = autorun.heartbeat_message(self._run(starters=starters), self._trigger(), self._games(), 2.0)
        assert "Jaxon Smith-Njigba (WR)" in body
        assert "Derrick Henry" not in body

    def test_says_so_when_no_starter_plays_the_slot(self):
        starters = [("RB", _player("Derrick Henry", "BAL", 16.1))]
        _, body = autorun.heartbeat_message(self._run(starters=starters), self._trigger(), self._games(), 2.0)
        assert "None of your starters play at 12:00" in body

    def test_reports_the_projected_lineup_total(self):
        starters = [("WR", _player("A", "SEA", 19.2)), ("RB", _player("B", "BAL", 16.1))]
        _, body = autorun.heartbeat_message(self._run(starters=starters), self._trigger(), self._games(), 2.0)
        assert "Projected lineup: 35.3 pts" in body

    def test_names_the_closest_declined_call(self):
        row = SimpleNamespace(
            add_name="Kansas City Chiefs", drop_name="Detroit Lions", position="DEF", kind="add", net=0.7,
            claim_note="WAIT FOR FREE AGENCY -- +0.7 is under the 1.4 a priority-5/12 slot is worth",
            decision=SimpleNamespace(week_gain=0.2),
        )
        _, body = autorun.heartbeat_message(self._run(waivers=[row]), self._trigger(), self._games(), 2.0)
        assert "Closest call: DEF Kansas City Chiefs for Detroit Lions, +0.2 pts this week (wait for free agency)" in body

    def test_a_sub_threshold_claim_says_why_it_stayed_quiet(self):
        run = self._run(waivers=[_waiver_candidate(add_name="Someone", net=0.6, claim=True)])
        _, body = autorun.notification_for(run, self._trigger(), self._cfg(), self._games())
        # The near miss moved into MONITOR, which is what that section is
        # for: close to a bar this week, could clear it next.
        assert "MONITOR" in body and "under the 2.0 bar" in body

    def _loaded(self, **sources):
        base = dict(projection_source="sleeper", roster_source="sleeper", slots_source="sleeper",
                    league_rosters_source="sleeper", season_ptd_source="off", ros_board=None, board=None,
                    cfg=SimpleNamespace(league=None))
        base.update(sources)
        return SimpleNamespace(**base)

    def test_all_live_feeds_answered(self):
        _, body = autorun.heartbeat_message(self._run(loaded=self._loaded()), self._trigger(), self._games(), 2.0)
        assert "every Sleeper feed answered" in body

    def test_a_silent_fallback_is_not_reported_as_all_clear(self):
        """The failure mode that would make the heartbeat a lie: projections
        quietly fell back to the frozen board, and the message said all
        clear anyway."""
        loaded = self._loaded(projection_source="board")
        _, body = autorun.heartbeat_message(self._run(loaded=loaded), self._trigger(), self._games(), 2.0)
        assert "NOT fully live" in body and "projection=board" in body


class TestResearchTrigger:
    """The research-only pass after Friday's final injury designations."""

    NOW = datetime(2026, 9, 10, 9, 0)  # a Thursday

    def test_a_friday_pass_is_built_when_research_is_on(self):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, research_weekday="fri", research_hour=17)
        research = [t for t in triggers if t.kind == "research"]
        assert len(research) == 1
        assert research[0].id == "research_2026-09-11"
        assert research[0].due_at == datetime(2026, 9, 11, 17, 0)

    def test_no_pass_when_research_is_off(self):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20)
        assert not any(t.kind == "research" for t in triggers)

    def test_a_bad_weekday_skips_the_pass_instead_of_crashing(self, capsys):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, research_weekday="friday")
        assert not any(t.kind == "research" for t in triggers)
        assert "friday" in capsys.readouterr().err

    def test_every_trigger_says_what_kind_it_is(self):
        k = datetime(2026, 9, 13, 13, 0)
        games = {"A": _game("B", True, k), "B": _game("A", False, k)}
        triggers = autorun.build_triggers(games, self.NOW, 80, "tue", 20, research_weekday="fri")
        assert {t.kind for t in triggers} == {"kickoff", "waiver", "research"}


class TestGradeTrigger:
    """The Tuesday-morning projection grade."""

    NOW = datetime(2026, 9, 10, 9, 0)  # a Thursday

    def test_a_tuesday_morning_grade_is_built_when_enabled(self):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, grade_weekday="tue", grade_hour=8)
        grade = [t for t in triggers if t.kind == "grade"]
        assert len(grade) == 1
        assert grade[0].id == "grade_2026-09-08"
        assert grade[0].due_at == datetime(2026, 9, 8, 8, 0)

    def test_no_grade_when_disabled(self):
        assert not any(t.kind == "grade" for t in autorun.build_triggers({}, self.NOW, 80, "tue", 20))

    def test_a_bad_weekday_skips_the_grade_instead_of_crashing(self, capsys):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, grade_weekday="tuesday")
        assert not any(t.kind == "grade" for t in triggers)
        assert "tuesday" in capsys.readouterr().err

    def _main_setup(self, tmp_path, monkeypatch, result):
        from scripts import grade_week

        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yml").write_text(
            "grade:\n  enabled: true\n  weekday: tue\n  hour: 8\nnotify:\n  channel: ntfy\n  ntfy_topic: t\n",
            encoding="utf-8",
        )
        far = datetime(2099, 1, 1, 13, 0)
        games = {"A": _game("B", True, far), "B": _game("A", False, far)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 2)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        monkeypatch.setattr(autorun, "eastern_to_local", lambda k: k)
        tuesday_morning = datetime(2026, 9, 15, 8, 30)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return tuesday_morning

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        def no_report(args):
            raise AssertionError("a grade trigger must not run the weekly report")

        monkeypatch.setattr(autorun.week_report, "run_report", no_report)
        calls, sent = [], []
        monkeypatch.setattr(grade_week, "scheduled_grade", lambda season, week, cfg: calls.append((season, week)) or result)
        monkeypatch.setattr("ffbot.notify.send", lambda cfg, title, body, **kw: sent.append((title, body)) or [])
        return calls, sent

    def test_main_fires_the_grade_records_it_and_pushes_it(self, tmp_path, monkeypatch):
        from scripts import grade_week

        message = ("ffbot W1: projections graded", "Week 1: ...")
        calls, sent = self._main_setup(tmp_path, monkeypatch, grade_week.ScheduledGrade(1, message, []))
        assert autorun.main(["--season", "2026"]) == 0
        assert calls == [(2026, 2)]
        assert sent == [message]
        state = json.loads((tmp_path / "data" / "autorun_state.json").read_text())
        assert "grade_2026-09-15" in state["2026-w02"]

    def test_a_grade_with_nothing_finished_is_retried_not_recorded(self, tmp_path, monkeypatch):
        from scripts import grade_week

        _calls, sent = self._main_setup(tmp_path, monkeypatch, grade_week.ScheduledGrade(None, None, ["no logged week has finished yet"]))
        assert autorun.main(["--season", "2026"]) == 0
        assert sent == []
        state_path = tmp_path / "data" / "autorun_state.json"
        assert not state_path.exists() or "grade_2026-09-15" not in state_path.read_text()


def _roster_player(name, team, pos, slot="WR", status=""):
    return SimpleNamespace(name=name, team=team, eligible_positions=[pos], selected_position=slot, status=status)


def _live_loaded(players):
    return SimpleNamespace(
        players=players, projection_source="sleeper", roster_source="sleeper", slots_source="sleeper",
        league_rosters_source="sleeper", season_ptd_source="off", ros_board=None, board=None,
        cfg=SimpleNamespace(league=None),
    )


class TestResearchContext:
    K1 = datetime(2026, 9, 13, 13, 0)
    K2 = datetime(2026, 9, 13, 16, 25)

    def _games(self):
        return {
            "SEA": _game("NE", True, self.K1), "NE": _game("SEA", False, self.K1),
            "BAL": _game("IND", False, self.K2), "IND": _game("BAL", True, self.K2),
        }

    def _run(self):
        players = [
            _roster_player("Jaxon Smith-Njigba", "SEA", "WR", "WR"),
            _roster_player("Derrick Henry", "BAL", "RB", "RB", "Q"),
            _roster_player("Bench Guy", "NE", "RB", "BN"),
        ]
        plan = SimpleNamespace(
            claims=[SimpleNamespace(add_name="David Montgomery", position="RB", add_team="DET")],
            adds=[SimpleNamespace(add_name="Kansas City Chiefs", position="DEF", add_team="KC")],
        )
        streamers = {"K": [SimpleNamespace(name="Kicker Ne", position="K", team="NE")]}
        return SimpleNamespace(week=2, loaded=_live_loaded(players), plan=plan, streamers=streamers)

    def test_a_slot_pass_covers_only_that_kickoffs_teams(self):
        trigger = autorun.Trigger(id="pre_kickoff_2026-09-13T13:00:00", due_at=self.K1, grace_minutes=110,
                                  label="x", kickoff=self.K1, local_kickoff=self.K1, kind="kickoff")
        ctx = autorun.research_context(trigger, self._run(), 2026, 2, self._games())
        assert ctx.mode == "slot"
        assert ctx.slot_teams == ("NE", "SEA")
        assert ctx.kickoff_et == "2026-09-13T13:00"
        roster = " | ".join(ctx.roster)
        assert "Jaxon Smith-Njigba (WR, SEA, starter" in roster
        assert "Bench Guy (RB, NE, bench" in roster
        assert "Derrick Henry" not in roster
        assert ctx.candidates == ("Kicker Ne (K, NE)",)

    def test_a_full_pass_covers_the_whole_roster_and_every_candidate(self):
        trigger = autorun.Trigger(id="pre_waiver_2026-09-15", due_at=self.K1, grace_minutes=720,
                                  label="x", kind="waiver")
        ctx = autorun.research_context(trigger, self._run(), 2026, 2, self._games())
        assert ctx.mode == "full" and ctx.slot_teams == () and ctx.kickoff_et == ""
        assert len(ctx.roster) == 3
        assert any("Derrick Henry (RB, BAL, starter, Sleeper status: Q)" == r for r in ctx.roster)
        assert ctx.candidates[:2] == ("David Montgomery (RB, DET)", "Kansas City Chiefs (DEF, KC)")


class TestResearchNotifications:
    def _run(self):
        brief = SimpleNamespace(lineup=SimpleNamespace(assignments=[], moves=[]))
        return wr_module.ReportRun(week=2, loaded=None, brief=brief, waivers=[], sections=["WEEK 2"])

    def _cfg(self, heartbeat=True):
        return SimpleNamespace(notify=SimpleNamespace(min_waiver_net=2.0, heartbeat=heartbeat))

    def _trigger(self, kind):
        k = datetime(2026, 9, 13, 13, 0)
        if kind == "kickoff":
            return autorun.Trigger(id="pre_kickoff_2026-09-13T13:00:00", due_at=k, grace_minutes=110,
                                   label="pre-kickoff check", kickoff=k, local_kickoff=k, kind="kickoff")
        if kind == "research":
            return autorun.Trigger(id="research_2026-09-11", due_at=k, grace_minutes=720,
                                   label="injury-report research (fri 17:00)", kind="research")
        return autorun.Trigger(id="pre_waiver_2026-09-15", due_at=k, grace_minutes=720,
                               label="pre-waiver check", kind="waiver")

    def _ok(self, **kw):
        from ffbot.research import ResearchResult

        return ResearchResult(ok=True, **kw)

    def _failed(self, alert="research didn't run: the Claude Code CLI isn't logged in (run `claude` in a terminal and /login)"):
        from ffbot.research import ResearchResult

        return ResearchResult(ok=False, alerts=[alert])

    def test_friday_research_says_what_it_found(self):
        title, body = autorun.notification_for(
            self._run(), self._trigger("research"), self._cfg(), {},
            research=self._ok(overrides=["Derrick Henry: O (nfl.com)"]),
        )
        assert "injury-report research" in title
        assert "1 official status(es): Derrick Henry: O (nfl.com)" in body
        assert "Status override: Derrick Henry: O (nfl.com)" in body

    def test_friday_research_success_is_quiet_with_heartbeat_off(self):
        assert autorun.notification_for(
            self._run(), self._trigger("research"), self._cfg(heartbeat=False), {}, research=self._ok(),
        ) is None

    def test_friday_research_failure_is_always_reported(self):
        title, body = autorun.notification_for(
            self._run(), self._trigger("research"), self._cfg(heartbeat=False), {}, research=self._failed(),
        )
        assert body.startswith("Research FAILED") and "/login" in body

    def test_a_quiet_waiver_check_still_reports_broken_research(self):
        title, body = autorun.notification_for(
            self._run(), self._trigger("waiver"), self._cfg(heartbeat=False), {}, research=self._failed(),
        )
        assert "Research FAILED" in body

    def test_a_quiet_waiver_check_with_working_research_says_so_in_its_no_claim_message(self):
        title, body = autorun.notification_for(
            self._run(), self._trigger("waiver"), self._cfg(), {}, research=self._ok(),
        )
        assert title.endswith("nothing worth a claim") and "Research: updated" in body
        assert autorun.notification_for(
            self._run(), self._trigger("waiver"), self._cfg(heartbeat=False), {}, research=self._ok(),
        ) is None

    def test_the_all_clear_says_research_ran(self):
        title, body = autorun.notification_for(
            self._run(), self._trigger("kickoff"), self._cfg(), {}, research=self._ok(),
        )
        assert "all clear" in title and "Research: updated" in body

    def test_an_actionable_message_carries_the_research_line(self):
        run = self._run()
        run.waivers = [_waiver_candidate(add_name="David Montgomery", net=55.4, claim=True)]
        title, body = autorun.notification_for(
            run, self._trigger("waiver"), self._cfg(), {}, research=self._ok(overrides=["X: O (nfl.com)"]),
        )
        assert "CLAIM David Montgomery" in body and "Research: updated" in body

    def test_an_unneeded_slot_pass_is_not_called_a_failure(self):
        line = autorun.research_line(self._failed("not needed -- none of your players or candidates play at 7:35PM"))
        assert line.startswith("Research: not needed")

    def test_downgraded_statuses_are_counted(self):
        assert "1 unverified kept as notes" in autorun.research_line(self._ok(downgraded=["X: O (https://x.com/a)"]))


class TestFireWithResearch:
    """End to end through `main`, with the report and the research run faked:
    research sits between a live report and a second report that reads it."""

    KICKOFF = datetime(2026, 9, 13, 20, 20)

    def _setup(self, tmp_path, monkeypatch, *, research_on=True, result=None, fail_report_call=None):
        monkeypatch.chdir(tmp_path)
        games = {"A": _game("B", True, self.KICKOFF), "B": _game("A", False, self.KICKOFF)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 1)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        monkeypatch.setattr(autorun, "eastern_to_local", lambda k: k)
        due_at = self.KICKOFF - timedelta(minutes=autorun.parse_args([]).lead_minutes)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return due_at

        monkeypatch.setattr(autorun, "datetime", _FrozenDatetime)

        self.report_calls, self.research_calls, self.sent = [], [], []

        def run_report(args):
            self.report_calls.append(args)
            if fail_report_call is not None and len(self.report_calls) == fail_report_call:
                raise RuntimeError("report blew up")
            run = _stub_run(week_num=args.week)
            run.loaded = _live_loaded([_roster_player("Starter A", "A", "WR", "WR")])
            return run

        monkeypatch.setattr(autorun.week_report, "run_report", run_report)

        from ffbot import research as research_mod

        outcome = result if result is not None else research_mod.ResearchResult(
            ok=True, overrides=["Starter A: O (nfl.com)"], transcript="Set Starter A to O.",
        )

        def fake_research(ctx, cfg, week_path, **kw):
            self.research_calls.append(ctx)
            return outcome

        monkeypatch.setattr("ffbot.research.run_research", fake_research)
        monkeypatch.setattr(
            "ffbot.notify.send", lambda cfg, title, body, **kw: self.sent.append((title, body)) or [],
        )
        config = "notify:\n  channel: ntfy\n  ntfy_topic: test-topic\n"
        if research_on:
            config += "research:\n  enabled: true\n"
        (tmp_path / "config.yml").write_text(config, encoding="utf-8")

    def _state(self, tmp_path):
        return json.loads((tmp_path / "data" / "autorun_state.json").read_text(encoding="utf-8"))["2026-w01"]

    def test_research_runs_between_a_live_report_and_a_rerun(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        assert autorun.main(["--season", "2026"]) == 0
        assert len(self.research_calls) == 1
        assert self.research_calls[0].mode == "slot"
        assert any("Starter A" in r for r in self.research_calls[0].roster)
        assert len(self.report_calls) == 2
        assert self.report_calls[0].refresh is True and self.report_calls[1].refresh is False

    def test_the_message_and_the_report_record_the_research(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        autorun.main(["--season", "2026"])
        assert len(self.sent) == 1
        assert "Research: updated" in self.sent[0][1] and "Starter A: O (nfl.com)" in self.sent[0][1]
        report = next((tmp_path / "reports").glob("*.md")).read_text(encoding="utf-8")
        assert "RESEARCH" in report and "Set Starter A to O." in report

    def test_failed_research_still_produces_the_check(self, tmp_path, monkeypatch):
        from ffbot.research import ResearchResult

        self._setup(tmp_path, monkeypatch, result=ResearchResult(ok=False, alerts=["research timed out after 15 min"]))
        assert autorun.main(["--season", "2026"]) == 0
        assert len(self.report_calls) == 1
        assert "pre_kickoff_2026-09-13T20:20:00" in self._state(tmp_path)
        assert "Research FAILED: research timed out" in self.sent[0][1]

    def test_a_retried_report_does_not_pay_for_research_again(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, fail_report_call=2)
        autorun.main(["--season", "2026"])
        state = self._state(tmp_path)
        assert "research:pre_kickoff_2026-09-13T20:20:00" in state
        assert "pre_kickoff_2026-09-13T20:20:00" not in state

        autorun.main(["--season", "2026"])  # the next poll retries the check
        assert len(self.research_calls) == 1
        assert len(self.report_calls) == 3
        assert "pre_kickoff_2026-09-13T20:20:00" in self._state(tmp_path)

    def test_research_off_runs_the_report_once(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, research_on=False)
        autorun.main(["--season", "2026"])
        assert self.research_calls == []
        assert len(self.report_calls) == 1

    def test_a_slot_with_nobody_on_it_skips_research(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            autorun.week_report, "run_report",
            lambda args: self.report_calls.append(args) or _stub_run(week_num=args.week),
        )
        autorun.main(["--season", "2026"])
        assert self.research_calls == []
        assert "Research: not needed" in self.sent[0][1]



class TestPostWaiverTrigger:
    """The free-agent check the morning after the weekly run."""

    NOW = datetime(2026, 9, 15, 20, 0)  # Tuesday evening

    def test_built_when_enabled(self):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, post_waiver_weekday="wed", post_waiver_hour=7)
        [post] = [t for t in triggers if t.kind == "post_waiver"]
        assert post.id == "post_waiver_2026-09-16"
        assert post.due_at == datetime(2026, 9, 16, 7, 0)
        assert post.label == "check (wed 07:00)"
        assert post.kickoff is None

    def test_absent_when_disabled(self):
        assert not any(t.kind == "post_waiver" for t in autorun.build_triggers({}, self.NOW, 80, "tue", 20))

    def test_a_bad_weekday_skips_it_loudly(self, capsys):
        triggers = autorun.build_triggers({}, self.NOW, 80, "tue", 20, post_waiver_weekday="wednesday")
        assert not any(t.kind == "post_waiver" for t in triggers)
        assert "wednesday" in capsys.readouterr().err

    def test_the_waiver_check_is_labelled_for_its_job(self):
        [waiver] = [t for t in autorun.build_triggers({}, self.NOW, 80, "tue", 20) if t.kind == "waiver"]
        assert waiver.label == "check (tue 20:00)" and waiver.id == "pre_waiver_2026-09-15"

    def test_every_kind_is_named(self):
        k = datetime(2026, 9, 20, 13, 0)
        games = {"A": _game("B", True, k), "B": _game("A", False, k)}
        triggers = autorun.build_triggers(
            games, self.NOW, 80, "tue", 20, research_weekday="fri", grade_weekday="tue", post_waiver_weekday="wed",
        )
        assert {t.kind for t in triggers} == {"kickoff", "waiver", "post_waiver", "research", "grade"}


class TestAutorunConfigBlock:
    """`autorun:` in config.yml sets both waiver-cycle checks; the flags the
    registered task passes still win, so nothing needs re-registering."""

    def _dry_run(self, tmp_path, monkeypatch, capsys, config_text, argv=()):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "config.yml").write_text(config_text, encoding="utf-8")
        far = datetime(2099, 1, 1, 13, 0)
        games = {"A": _game("B", True, far), "B": _game("A", False, far)}
        monkeypatch.setattr(autorun, "current_week", lambda season: 2)
        monkeypatch.setattr(autorun, "this_week_games", lambda season, week: games)
        assert autorun.main(["--dry-run", "--season", "2026", *argv]) == 0
        return capsys.readouterr()

    def test_config_sets_both_checks(self, tmp_path, monkeypatch, capsys):
        out = self._dry_run(
            tmp_path, monkeypatch, capsys,
            "autorun:\n  waiver_weekday: mon\n  waiver_hour: 21\n  post_waiver_enabled: true\n"
            "  post_waiver_weekday: wed\n  post_waiver_hour: 7\n",
        ).out
        assert "check (mon 21:00)" in out and "check (wed 07:00)" in out

    def test_the_command_line_flags_override_the_config(self, tmp_path, monkeypatch, capsys):
        out = self._dry_run(
            tmp_path, monkeypatch, capsys, "autorun:\n  waiver_weekday: mon\n  waiver_hour: 21\n",
            argv=["--waiver-weekday", "tue", "--waiver-hour", "20"],
        ).out
        assert "check (tue 20:00)" in out and "(mon 21:00)" not in out

    def test_the_free_agent_check_is_off_unless_enabled(self, tmp_path, monkeypatch, capsys):
        out = self._dry_run(tmp_path, monkeypatch, capsys, "autorun:\n  post_waiver_weekday: wed\n").out
        assert "post_waiver_" not in out

    def test_a_bad_config_weekday_falls_back_loudly(self, tmp_path, monkeypatch, capsys):
        captured = self._dry_run(tmp_path, monkeypatch, capsys, "autorun:\n  waiver_weekday: tuesday\n")
        assert "tuesday" in captured.err and "check (tue 20:00)" in captured.out
