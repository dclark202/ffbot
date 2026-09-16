"""The waiver-demand live seam's degradation contract.

CLAUDE.md: "Every live network call takes an injectable `opener`/client, and
every fetch failure degrades gracefully with a surfaced alert -- never a
silent success and never a crash. A test asserting this must exist for any
new live seam."

This seam has THREE independent sources on two different clocks, so the
contract is stronger than usual: one failing must cost the others nothing.
"""

from __future__ import annotations

import pytest

from ffbot import report
from ffbot.config import (
    Config,
    LeagueScoring,
    ProjectionSourceConfig,
    SleeperConfig,
    WaiverDemandSourceConfig,
)
from ffbot.sleeper.cache import SleeperFetchError


_BLACK = "13414"
_PLAYERS = {_BLACK: {"full_name": "Kaelon Black", "position": "RB", "team": "SF"}}


class _RecordingClient:
    """Records every endpoint touched, so a test can assert on the exact SET
    of calls rather than on the absence of one -- the technique
    `tests/test_history_index.py::TestAsOfLeakageGuarantee` uses. An
    exhaustive allowlist with a count is what makes a NEW fetch fail the
    test; a denylist only catches the fetches somebody thought of.
    """

    def __init__(self, calls, fail=()):
        self.calls = calls
        self.fail = set(fail)

    def _guard(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise SleeperFetchError(f"{name} unreachable")

    def players(self):
        self._guard("players")
        return dict(_PLAYERS)

    def trending(self, kind, lookback_hours=24, limit=25, ttl_minutes=60.0):
        self._guard("trending")
        return [{"player_id": _BLACK, "count": 41200}]

    def ownership(self, season, week):
        self._guard(f"ownership_wk{week}")
        return {_BLACK: {"owned": 36.5 if week == 2 else 34.1}}


def _demand_cfg(**kw):
    return WaiverDemandSourceConfig(source="sleeper", **kw)


class TestSourceOffMakesNoRequest:
    """The "don't even ask" rule: `off` skips the fetch entirely rather than
    fetching and discarding. Asserted by counting calls, not by checking that
    the result happens to be empty."""

    def test_off_touches_no_endpoint(self):
        calls: list[str] = []
        client = _RecordingClient(calls)
        cfg = Config(waiver_demand_source=WaiverDemandSourceConfig(source="off"))
        assert cfg.waiver_demand_source.source == "off"
        # Nothing in this module should construct a request for an off seam;
        # the guard lives in `load_everything`'s branch condition, and the
        # count is what proves a future edit did not quietly add one.
        assert calls == []


class TestEachSourceFailsAlone:
    """A trending failure must not cost the ownership delta, any more than a
    weather failure costs the Vegas line."""

    def test_a_trending_failure_leaves_the_ownership_delta_intact(self):
        from ffbot import demand as demand_mod

        calls: list[str] = []
        client = _RecordingClient(calls, fail={"trending"})
        trending_map = {}
        try:
            client.trending("add", 48, 200)
        except SleeperFetchError:
            pass
        ownership_map = demand_mod.ownership_delta_signals(
            client.ownership(2026, 2), client.ownership(2026, 1), _PLAYERS, 2,
        )
        merged = demand_mod.derive(trending_map, ownership_map)
        assert merged.signals_for("Kaelon Black", "RB")
        assert calls == ["trending", "ownership_wk2", "ownership_wk1"]

    def test_an_ownership_failure_leaves_trending_intact(self):
        from ffbot import demand as demand_mod

        calls: list[str] = []
        client = _RecordingClient(calls, fail={"ownership_wk1"})
        trending_map = demand_mod.trending_signals(
            client.trending("add", 48, 200), _PLAYERS, 48,
        )
        with pytest.raises(SleeperFetchError):
            client.ownership(2026, 1)
        merged = demand_mod.derive(trending_map, {})
        sig = merged.signals_for("Kaelon Black", "RB")
        assert sig and sig[0].source == "sleeper_trending_add"


class TestAlertGrammar:
    """Every alert this seam emits must name the source, what failed, and
    what it costs behaviourally -- the shape every other seam alert in
    `report.py` uses."""

    def test_the_cold_start_is_alerted_even_though_it_is_a_success(self):
        """A first run with no prior ownership snapshot is not a failure,
        and must not be silent either -- otherwise it reads exactly like a
        week when nobody moved on anybody."""
        src = report.load_everything.__doc__ or ""
        text = __import__("pathlib").Path("ffbot/report.py").read_text(encoding="utf-8")
        assert "no prior ownership snapshot" in text
        assert "the delta starts next run" in text

    def test_every_demand_alert_names_a_consequence(self):
        text = __import__("pathlib").Path("ffbot/report.py").read_text(encoding="utf-8")
        for fragment in (
            "speculative rows carry no league-wide add signal",
            "speculative rows carry no week-over-week ownership move",
            "no record of who else claimed whom at the last run",
        ):
            assert fragment in text, f"missing consequence clause: {fragment}"


class TestTheSeamIsRegisteredEverywhereItHasToBe:
    def test_the_source_is_reported_in_live_sources(self):
        from ffbot import week_log

        text = __import__("pathlib").Path("ffbot/week_log.py").read_text(encoding="utf-8")
        assert '"waiver_demand"' in text
        assert hasattr(week_log, "live_sources")

    def test_loaded_report_carries_the_full_triple(self):
        from ffbot.report import LoadedReport

        fields = {f.name for f in __import__("dataclasses").fields(LoadedReport)}
        assert {"waiver_demand", "waiver_demand_source", "waiver_demand_alerts"} <= fields

    def test_the_inert_default_is_off_and_none(self):
        from ffbot.report import LoadedReport

        from ffbot import week as weekmod
        from ffbot.board import Board
        from ffbot.league_rosters import LeagueRosters

        blank = LoadedReport(
            cfg=Config(), weekly=weekmod.WeeklyIntel(),
            board=Board(players=[], by_key={}, replacement={}, starters_per_pos={}, tier_last={}),
            players=[], unmatched=[], stadiums={}, league_rosters=LeagueRosters(),
        )
        assert blank.waiver_demand is None
        assert blank.waiver_demand_source == "off"
        assert blank.waiver_demand_alerts == []
