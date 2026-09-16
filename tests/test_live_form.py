"""The live wire for the five Validated-but-inert form dials.

Two things have to hold. The math must be the SAME math the backtest
measured -- one implementation, two feeds, asserted rather than assumed --
and the seam must degrade loudly, because the failure this module exists to
end is precisely a signal that goes quiet and says nothing.
"""

from __future__ import annotations

import pytest

from ffbot import form as form_math
from ffbot.config import Config, LeagueScoring, SeasonConfig
from ffbot.live import form as live_form


_SETTINGS = {"rec": 0.5, "rec_yd": 0.1, "rush_yd": 0.1, "rush_td": 6.0, "rec_td": 6.0}


def _cfg() -> Config:
    return Config(league=LeagueScoring(sleeper_scoring_settings=dict(_SETTINGS)))


def _row(name, position, team, **stats):
    return {"name": name, "position": position, "team": team, "sleeper_stats": stats}


class TestBothFeedsShareOneImplementation:
    """The shipped weights were selected against ONE definition of these
    signals. A live path computing a slightly different one would be running
    an untested configuration while claiming a validated one."""

    def test_the_historical_provider_calls_the_shared_math(self):
        from ffbot.history import signals

        assert signals._percentile_rank_within_position is form_math.percentile_rank_within_position
        assert signals._USAGE_POSITIONS is form_math.USAGE_POSITIONS

    def test_there_is_no_second_copy_of_the_trend_formula(self):
        """`recent mean / season mean` must appear once, in ffbot/form.py."""
        import pathlib

        offenders = []
        for p in pathlib.Path("ffbot").rglob("*.py"):
            if p.name == "form.py" and p.parent.name == "ffbot":
                continue
            text = p.read_text(encoding="utf-8")
            if "recent_avg / season_avg" in text:
                offenders.append(str(p))
        assert offenders == [], f"trend formula duplicated in {offenders}"

    def test_a_rising_player_outranks_a_flat_one_outranks_a_fading_one(self):
        """Four games, so the three-game recent window actually differs from
        the season mean."""
        log = {
            "a:WR": [(1, 5.0), (2, 5.0), (3, 15.0), (4, 20.0)],
            "b:WR": [(1, 10.0), (2, 10.0), (3, 10.0), (4, 10.0)],
            "c:WR": [(1, 20.0), (2, 15.0), (3, 5.0), (4, 5.0)],
        }
        direct = form_math.scoring_trend_scores(log)
        assert direct["a"]["momentum"] > direct["b"]["momentum"] > direct["c"]["momentum"]


class TestTiesDoNotManufactureARanking:
    """With `min_games == recent_games == 3`, every player's trend in week 4
    is identically 1.0 -- the recent window IS the season. Spreading that
    across 0-100 by dict order would make the first week these dials ever
    speak pure noise."""

    def test_identical_trends_all_score_the_neutral_midpoint(self):
        log = {f"p{i}:WR": [(1, 10.0 * (i + 1)), (2, 5.0), (3, 7.0)] for i in range(5)}
        out = form_math.scoring_trend_scores(log)
        assert {v["momentum"] for v in out.values()} == {50.0}

    def test_a_partial_tie_shares_the_average_rank(self):
        raw = {"a": 1.0, "b": 2.0, "c": 2.0, "d": 3.0}
        pos = {k: "WR" for k in raw}
        out = form_math.percentile_rank_within_position(raw, pos)
        assert out["a"] == 0.0
        assert out["b"] == out["c"] == pytest.approx(50.0)  # ranks 1 and 2 averaged
        assert out["d"] == 100.0

    def test_genuinely_differing_values_are_unchanged(self):
        raw = {"a": 1.0, "b": 2.0, "c": 3.0}
        out = form_math.percentile_rank_within_position(raw, {k: "WR" for k in raw})
        assert (out["a"], out["b"], out["c"]) == (0.0, 50.0, 100.0)


class TestWoprMatchesTheDefinition:
    def test_it_is_target_share_and_air_yards_share_in_the_published_ratio(self):
        rows = [
            _row("Alpha Wr", "WR", "SF", rec_tgt=6.0, rec_air_yd=80.0),
            _row("Beta Wr", "WR", "SF", rec_tgt=4.0, rec_air_yd=20.0),
        ]
        _points, usage = live_form.build_logs({1: rows}, _cfg().league)
        # Alpha: 60% of targets, 80% of air yards.
        expected = 1.5 * 0.6 + 0.7 * 0.8
        assert usage["alpha wr:WR"][0][1] == pytest.approx(expected)

    def test_shares_are_measured_per_week_against_that_week_s_offense(self):
        """Not against a season aggregate -- a trade or an injury makes that
        meaningless."""
        rows_wk1 = [_row("Alpha Wr", "WR", "SF", rec_tgt=10.0, rec_air_yd=100.0)]
        rows_wk2 = [
            _row("Alpha Wr", "WR", "SF", rec_tgt=5.0, rec_air_yd=50.0),
            _row("Newcomer Wr", "WR", "SF", rec_tgt=5.0, rec_air_yd=50.0),
        ]
        _p, usage = live_form.build_logs({1: rows_wk1, 2: rows_wk2}, _cfg().league)
        assert usage["alpha wr:WR"][0][1] == pytest.approx(1.5 + 0.7)   # 100% share
        assert usage["alpha wr:WR"][1][1] == pytest.approx(1.5 * 0.5 + 0.7 * 0.5)

    def test_a_team_with_no_passing_game_records_no_share(self):
        """Zero of zero is not a share, and recording it as 0.0 would read as
        'his role collapsed'."""
        rows = [_row("Alpha Wr", "WR", "SF", rec_tgt=0.0, rec_air_yd=0.0)]
        _p, usage = live_form.build_logs({1: rows}, _cfg().league)
        assert "alpha wr:WR" not in usage

    def test_only_receiving_positions_get_a_usage_entry(self):
        rows = [
            _row("Some Qb", "QB", "SF", pass_yd=300.0),
            _row("Some K", "K", "SF", fgm=3.0),
            _row("Some Wr", "WR", "SF", rec_tgt=8.0, rec_air_yd=90.0),
        ]
        _p, usage = live_form.build_logs({1: rows}, _cfg().league)
        assert set(usage) == {"some wr:WR"}


class TestPointsAreLeagueScored:
    def test_it_uses_the_league_settings_not_pts_ppr(self):
        """`momentum` and `volatility` describe what a player did for THIS
        team, and a PPR total is a different number in a league that pays
        for first downs."""
        rows = [_row("Alpha Wr", "WR", "SF", rec=4.0, rec_yd=50.0)]
        points, _u = live_form.build_logs({1: rows}, _cfg().league)
        # 4 rec x 0.5 + 50 yd x 0.1
        assert points["alpha wr:WR"][0][1] == pytest.approx(4 * 0.5 + 50 * 0.1)


class TestThreeGamesAreRequired:
    """Not incidental: every signal needs three completed games, so all five
    dials are structurally silent through week 3 of any season."""

    def _weeks(self, n):
        return {
            wk: [
                _row("Alpha Wr", "WR", "SF", rec=5.0, rec_yd=60.0, rec_tgt=8.0, rec_air_yd=90.0),
                _row("Beta Wr", "WR", "SF", rec=2.0, rec_yd=20.0, rec_tgt=4.0, rec_air_yd=30.0),
            ]
            for wk in range(1, n + 1)
        }

    def test_two_weeks_produces_nothing_and_says_why(self):
        weeks = self._weeks(2)
        scores, alerts = live_form.live_form_signals(
            2026, 3, _cfg(), lambda wk: weeks.get(wk, []),
        )
        assert scores == {}
        assert any("silent until week 4" in a for a in alerts)

    def test_three_weeks_produces_every_signal_family(self):
        weeks = self._weeks(3)
        scores, alerts = live_form.live_form_signals(
            2026, 4, _cfg(), lambda wk: weeks.get(wk, []),
        )
        assert scores, "three completed weeks must produce signals"
        produced = set().union(*(set(v) for v in scores.values()))
        assert {"volatility", "upside", "momentum", "usage", "divergence"} <= produced
        assert alerts == []

    def test_only_weeks_strictly_before_the_target_are_read(self):
        """Leakage: a signal computed from the week it is predicting would be
        worthless and would quietly flatter every grade."""
        seen = []

        def fetch(wk):
            seen.append(wk)
            return []

        live_form.live_form_signals(2026, 4, _cfg(), fetch)
        assert seen == [1, 2, 3]


class TestItDegradesLoudly:
    def test_a_failing_week_is_skipped_with_an_alert_and_the_rest_survive(self):
        weeks = {
            wk: [
                _row("Alpha Wr", "WR", "SF", rec=5.0, rec_yd=60.0, rec_tgt=8.0, rec_air_yd=90.0),
                _row("Beta Wr", "WR", "SF", rec=1.0, rec_yd=5.0, rec_tgt=2.0, rec_air_yd=10.0),
            ]
            for wk in (1, 2, 3, 4)
        }

        def fetch(wk):
            if wk == 2:
                raise RuntimeError("endpoint down")
            return weeks[wk]

        scores, alerts = live_form.live_form_signals(2026, 5, _cfg(), fetch)
        assert scores, "three good weeks out of four is still a real signal"
        assert any("could not read week 2" in a for a in alerts)

    def test_no_league_scoring_settings_means_inert_not_a_season_of_zeroes(self):
        cfg = Config(league=LeagueScoring())
        scores, alerts = live_form.live_form_signals(2026, 5, cfg, lambda wk: [])
        assert scores == {}
        assert any("no league scoring settings" in a for a in alerts)


class TestResearchStillWins:
    def test_a_hand_written_field_is_not_overwritten(self):
        from ffbot import week
        from ffbot.report import _merge_form_scores

        weekly = week.WeeklyIntel(
            players={"alpha wr": week.WeeklyPlayerIntel(name="Alpha Wr", usage_trend=80.0)}
        )
        merged = _merge_form_scores(weekly, {"alpha wr": {"usage": 10.0, "momentum": 30.0}})
        entry = merged.players["alpha wr"]
        assert entry.usage_trend == 80.0   # the human's number survives
        assert entry.momentum == 30.0      # the empty field gets filled

    def test_a_player_with_no_entry_gets_one(self):
        from ffbot import week
        from ffbot.report import _merge_form_scores

        merged = _merge_form_scores(week.WeeklyIntel(), {"alpha wr": {"usage": 60.0}})
        assert merged.players["alpha wr"].usage_trend == 60.0

    def test_an_empty_score_map_is_an_exact_no_op(self):
        from ffbot import week
        from ffbot.report import _merge_form_scores

        weekly = week.WeeklyIntel(players={"a": week.WeeklyPlayerIntel(name="A")})
        assert _merge_form_scores(weekly, {}) is weekly


class TestTheSeamIsRegistered:
    def test_the_source_is_reported_and_the_alerts_reach_both_surfaces(self):
        from pathlib import Path

        assert '"form"' in Path("ffbot/week_log.py").read_text(encoding="utf-8")
        for f in ("ffbot/webapi.py", "scripts/week_report.py"):
            assert "loaded.form_alerts" in Path(f).read_text(encoding="utf-8")

    def test_it_ships_off_in_code_and_on_in_the_narrated_config(self):
        """Code-level defaults are offline; `config.yml` is live-by-default."""
        from ffbot.config import FormSourceConfig

        assert FormSourceConfig().source == "off"
        assert Config.load("config.yml").form_source.source == "sleeper"


class TestTheWireActuallyReachesTheEngine:
    """The point of all of this. `_momentum_multiplier` returned exactly 1.0
    for every player, every run, for a whole season; `spice_bonus`'s variance
    terms contributed exactly 0.0. These assert that stops being true once
    the feed is connected -- a test that the dials DO something, which is the
    thing nobody had."""

    def _weeks(self):
        # Four weeks so the three-game recent window differs from the season
        # mean; Alpha's role and scoring are climbing, Beta's are fading.
        climb = [(1, 1.0), (2, 1.0), (3, 6.0), (4, 9.0)]
        fade = [(1, 9.0), (2, 6.0), (3, 1.0), (4, 1.0)]
        weeks = {}
        for wk in range(1, 5):
            a = dict(climb)[wk]
            b = dict(fade)[wk]
            weeks[wk] = [
                _row("Alpha Wr", "WR", "SF", rec=a, rec_yd=a * 10, rec_tgt=a, rec_air_yd=a * 12),
                _row("Beta Wr", "WR", "SF", rec=b, rec_yd=b * 10, rec_tgt=b, rec_air_yd=b * 12),
            ]
        return weeks

    def _weekly_with_signals(self):
        from ffbot.report import _merge_form_scores
        from ffbot import week

        weeks = self._weeks()
        scores, _alerts = live_form.live_form_signals(
            2026, 5, _cfg(), lambda wk: weeks.get(wk, []),
        )
        assert scores, "the feed produced nothing; the rest of this test is vacuous"
        return _merge_form_scores(week.WeeklyIntel(), scores), scores

    def test_momentum_multiplier_is_no_longer_a_flat_one(self):
        from ffbot import week

        weekly, _ = self._weekly_with_signals()
        cfg = SeasonConfig(usage_weight=0.15, momentum_weight=0.15, divergence_weight=0.05)
        rising = week._momentum_multiplier(weekly.players["alpha wr"], cfg)
        fading = week._momentum_multiplier(weekly.players["beta wr"], cfg)
        assert rising != 1.0, "the wire is not connected: the multiplier is still a no-op"
        assert rising > fading, "a rising role must not score below a fading one"

    def test_the_variance_terms_stop_contributing_exactly_zero(self):
        from ffbot import week
        from ffbot.models import Player

        weekly, _ = self._weekly_with_signals()
        cfg = SeasonConfig(volatility_weight=0.05, upside_lean_weight=0.05)
        player = Player(
            player_id=1, name="Alpha Wr", eligible_positions=["WR"],
            selected_position="WR", team="SF", projected_points=12.0,
        )
        bonus = week.spice_bonus(player, weekly, cfg, scale=8.0)
        assert bonus != 0.0, "volatility/upside are still structurally inert"

    def test_an_unwired_run_is_still_exactly_the_old_behaviour(self):
        """`form_source: off` must leave the engine bit-identical to before
        this feed existed -- the fallback has to stay a true no-op."""
        from ffbot import week

        cfg = SeasonConfig(usage_weight=0.15, momentum_weight=0.15)
        assert week._momentum_multiplier(None, cfg) == 1.0
