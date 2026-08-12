from __future__ import annotations

import dataclasses

import pytest

from ffbot import week
from ffbot.board import Board
from ffbot.config import Config, DraftConfig, LeagueScoring, SeasonConfig, TeamStanding
from ffbot.league_rosters import LeagueRosters
from ffbot.models import IR_SLOTS, Player
from tests.conftest import mk_bp


def _p(name, pos, proj=10.0, status="", team="BUF", **kw) -> Player:
    return Player(
        player_id=hash(name) % 100000,
        name=name,
        eligible_positions=[pos] if isinstance(pos, str) else pos,
        team=team,
        projected_points=proj,
        status=status,
        **kw,
    )


def _spicy(**kw) -> SeasonConfig:
    defaults = dict(
        weather_weight=0.5, vegas_weight=0.4, volatility_weight=0.3,
        upside_lean_weight=0.3, streaming_weight=0.8,
    )
    defaults.update(kw)
    return SeasonConfig(**defaults)


DOME = {"NO": week.StadiumInfo(dome=True)}
OUTDOOR = {"BUF": week.StadiumInfo(dome=False), "MIA": week.StadiumInfo(dome=False)}


class TestLoadWeeklyIntel:
    def test_missing_file_is_empty_not_an_error(self):
        w = week.load_weekly_intel("does/not/exist.yml")
        assert w.players == {} and w.games == {}

    def test_full_player_entry(self, tmp_path):
        path = tmp_path / "w.yml"
        path.write_text(
            "week: 3\n"
            "players:\n"
            '  "Some Guy":\n'
            "    status: Q\n"
            "    upside: 70\n"
            "    risk: 20\n"
            "    volatility: 60\n"
            "    usage_trend: 55\n"
            "    momentum: 45\n"
            "    divergence: 35\n"
            '    note: "limited in practice"\n'
            "    flags: [trending-up]\n",
            encoding="utf-8",
        )
        w = week.load_weekly_intel(path)
        assert w.week == 3
        entry = w.players["some guy"]
        assert entry.status == "Q"
        assert entry.upside == 70.0
        assert entry.risk == 20.0
        assert entry.volatility == 60.0
        assert entry.usage_trend == 55.0
        assert entry.momentum == 45.0
        assert entry.divergence == 35.0
        assert entry.note == "limited in practice"
        assert entry.flags == ("trending-up",)

    def test_bare_string_is_a_note(self, tmp_path):
        path = tmp_path / "w.yml"
        path.write_text('players:\n  "Some Guy": "hot take"\n', encoding="utf-8")
        assert week.load_weekly_intel(path).players["some guy"].note == "hot take"

    def test_games_section(self, tmp_path):
        path = tmp_path / "w.yml"
        path.write_text(
            "games:\n"
            "  BUF:\n"
            "    opponent: MIA\n"
            '    kickoff_et: "2026-09-14T13:00"\n'
            "    home: true\n"
            "    wind_mph: 22\n"
            "    team_total: 27.5\n"
            "    opp_total: 19.0\n",
            encoding="utf-8",
        )
        g = week.load_weekly_intel(path).games["BUF"]
        assert g.opponent == "MIA" and g.home is True and g.wind_mph == 22 and g.team_total == 27.5

    def test_out_of_range_score_rejected(self, tmp_path):
        path = tmp_path / "w.yml"
        path.write_text('players:\n  "X":\n    upside: 140\n', encoding="utf-8")
        with pytest.raises(week.WeeklyIntelError):
            week.load_weekly_intel(path)

    def test_game_without_opponent_rejected(self, tmp_path):
        path = tmp_path / "w.yml"
        path.write_text("games:\n  BUF:\n    wind_mph: 10\n", encoding="utf-8")
        with pytest.raises(week.WeeklyIntelError):
            week.load_weekly_intel(path)


class TestUnmatchedWarnings:
    def test_flags_a_name_matching_nobody(self):
        roster = [_p("Josh Allen", "QB")]
        w = week.WeeklyIntel(players={"a typo name": week.WeeklyPlayerIntel(name="A Typo Name")})
        warnings = week.unmatched_player_warnings(roster, w)
        assert len(warnings) == 1
        assert "A Typo Name" in warnings[0]

    def test_matched_name_is_silent(self):
        roster = [_p("Josh Allen", "QB")]
        w = week.WeeklyIntel(players={"josh allen": week.WeeklyPlayerIntel(name="Josh Allen")})
        assert week.unmatched_player_warnings(roster, w) == []


class TestStatusOverride:
    def test_weekly_status_wins(self):
        roster = [_p("X", "WR", status="")]
        w = week.WeeklyIntel(players={"x": week.WeeklyPlayerIntel(name="X", status="O")})
        out = week.apply_status_overrides(roster, w)
        assert out[0].status == "O"

    def test_blank_override_leaves_existing_status(self):
        # A blank status field means "no claim," not "healthy" -- it must not
        # clobber a status the roster source already knew about.
        roster = [_p("X", "WR", status="Q")]
        w = week.WeeklyIntel(players={"x": week.WeeklyPlayerIntel(name="X", status="")})
        out = week.apply_status_overrides(roster, w)
        assert out[0].status == "Q"

    def test_no_entry_is_a_noop(self):
        roster = [_p("X", "WR", status="Q")]
        out = week.apply_status_overrides(roster, week.WeeklyIntel())
        assert out[0].status == "Q"


class TestLoadStadiums:
    def test_missing_file_is_empty(self, tmp_path):
        assert week.load_stadiums(tmp_path / "does_not_exist.yml") == {}

    def test_every_real_nfl_team_abbreviation_is_covered(self):
        # Regression test for the class of bug that let BAL slip out of the
        # file silently: `is_dome_game` fails OPEN (treats an unknown
        # abbreviation as a dome) rather than raising, so a missing team
        # produces a plausible number instead of an error. This is the one
        # place that gap would actually be caught.
        from ffbot.names import NFL_TEAMS

        stadiums = week.load_stadiums()
        abbreviations = set(NFL_TEAMS.values())
        missing = abbreviations - set(stadiums.keys())
        assert not missing, f"data/stadiums.yml is missing: {sorted(missing)}"


class TestDomeDetection:
    def test_home_dome_game_is_a_dome(self):
        game = week.GameInfo(opponent="BUF", home=True)
        assert week.is_dome_game("NO", game, DOME) is True

    def test_away_game_uses_opponents_stadium(self):
        # BUF at NO is a dome game for the Bills even though Highmark isn't one.
        game = week.GameInfo(opponent="NO", home=False)
        assert week.is_dome_game("BUF", game, DOME) is True

    def test_outdoor_road_game_is_not_a_dome(self):
        game = week.GameInfo(opponent="MIA", home=False)
        assert week.is_dome_game("BUF", game, OUTDOOR) is False

    def test_unknown_stadium_is_treated_as_neutral(self):
        # Missing data is a data gap, not evidence of bad weather.
        assert week.is_dome_game("ZZZ", week.GameInfo(opponent="YYY"), {}) is True

    def test_no_game_info_is_treated_as_dome(self):
        assert week.is_dome_game("BUF", None, OUTDOOR) is True

    def test_venue_override_wins_over_home_away_derivation(self):
        # BUF hosting, so home/away derivation alone would look up BUF's own
        # (outdoor) stadium -- the venue override must take precedence.
        stadiums = {**OUTDOOR, "LONDON_TOT": week.StadiumInfo(dome=False)}
        game = week.GameInfo(opponent="MIA", home=True, venue="LONDON_TOT")
        assert week.is_dome_game("BUF", game, stadiums) is False

    def test_venue_override_can_be_a_dome_even_for_an_outdoor_home_team(self):
        stadiums = {**OUTDOOR, "SOME_DOME_VENUE": week.StadiumInfo(dome=True)}
        game = week.GameInfo(opponent="MIA", home=True, venue="SOME_DOME_VENUE")
        assert week.is_dome_game("BUF", game, stadiums) is True

    def test_no_venue_falls_back_to_home_away_derivation(self):
        game = week.GameInfo(opponent="BUF", home=True)
        assert week.is_dome_game("NO", game, DOME) is True


class TestWeatherSeverity:
    def test_below_both_thresholds_is_exactly_zero(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", wind_mph=8.0, precip_pct=10.0)
        assert week.weather_severity(game, cfg) == 0.0

    def test_wind_above_threshold_is_positive(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", wind_mph=30.0)
        assert week.weather_severity(game, cfg) > 0.0

    def test_no_game_is_zero(self):
        assert week.weather_severity(None, _spicy()) == 0.0

    def test_capped_at_one(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", wind_mph=200.0, precip_pct=100.0)
        assert week.weather_severity(game, cfg) == 1.0


class TestWeatherMultiplier:
    def test_dome_game_is_untouched_regardless_of_forecast(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="BUF", home=True, wind_mph=40.0)
        assert week.weather_multiplier("QB", "NO", game, cfg, DOME) == 1.0

    def test_bad_weather_discounts_qb(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", home=True, wind_mph=30.0)
        assert week.weather_multiplier("QB", "BUF", game, cfg, OUTDOOR) < 1.0

    def test_rb_discounted_less_than_qb_in_the_same_game(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", home=True, wind_mph=30.0)
        qb_mult = week.weather_multiplier("QB", "BUF", game, cfg, OUTDOOR)
        rb_mult = week.weather_multiplier("RB", "BUF", game, cfg, OUTDOOR)
        assert rb_mult > qb_mult

    def test_def_is_never_discounted(self):
        # Bad weather tends to help defenses (more punts/turnovers), so
        # applying the offense penalty here would be backwards.
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", home=True, wind_mph=40.0, precip_pct=90.0)
        assert week.weather_multiplier("DEF", "BUF", game, cfg, OUTDOOR) == 1.0

    def test_zero_weight_is_a_noop(self):
        cfg = _spicy(weather_weight=0.0)
        game = week.GameInfo(opponent="MIA", home=True, wind_mph=40.0)
        assert week.weather_multiplier("QB", "BUF", game, cfg, OUTDOOR) == 1.0

    def test_calm_weather_is_a_noop(self):
        cfg = _spicy()
        game = week.GameInfo(opponent="MIA", home=True, wind_mph=5.0, precip_pct=0.0)
        assert week.weather_multiplier("QB", "BUF", game, cfg, OUTDOOR) == 1.0


class TestVenueDisruptionMultiplier:
    def test_zero_weight_is_an_exact_noop_by_default(self):
        # venue_disruption_weight defaults to 0.0 and is deliberately not
        # part of any SPICE_PRESETS level -- must stay a no-op even on an
        # international game unless explicitly set.
        cfg = _spicy()  # spicy on every other dial, venue_disruption_weight untouched
        game = week.GameInfo(opponent="MIA", international=True)
        assert week.venue_disruption_multiplier("QB", game, cfg) == 1.0

    def test_international_game_discounts_offense(self):
        cfg = _spicy(venue_disruption_weight=0.2)
        game = week.GameInfo(opponent="MIA", international=True)
        assert week.venue_disruption_multiplier("QB", game, cfg) == pytest.approx(0.8)

    def test_def_is_never_discounted(self):
        cfg = _spicy(venue_disruption_weight=0.2)
        game = week.GameInfo(opponent="MIA", international=True)
        assert week.venue_disruption_multiplier("DEF", game, cfg) == 1.0

    def test_non_international_game_is_a_noop(self):
        cfg = _spicy(venue_disruption_weight=0.2)
        game = week.GameInfo(opponent="MIA", international=False)
        assert week.venue_disruption_multiplier("QB", game, cfg) == 1.0

    def test_no_game_is_a_noop(self):
        cfg = _spicy(venue_disruption_weight=0.2)
        assert week.venue_disruption_multiplier("QB", None, cfg) == 1.0

    def test_applies_to_both_teams_symmetrically(self):
        # Both sides of an international game are disrupted alike -- there's
        # no "traveling team" distinction in the multiplier itself.
        cfg = _spicy(venue_disruption_weight=0.15)
        game = week.GameInfo(opponent="MIA", international=True)
        home_side = week.venue_disruption_multiplier("WR", game, cfg)
        away_side = week.venue_disruption_multiplier("WR", dataclasses.replace(game, opponent="BUF"), cfg)
        assert home_side == away_side


class TestVegasMultiplier:
    def test_high_implied_total_lifts_offense(self):
        # Two games, not one: with only BUF's game loaded, the league average
        # would be computed FROM that same number, making delta trivially 0
        # regardless of how "high" the total is. A second game is what gives
        # the average something independent to sit below.
        cfg = _spicy()
        w = week.WeeklyIntel(games={
            "BUF": week.GameInfo(opponent="MIA", team_total=32.0, opp_total=18.0),
            "OTH": week.GameInfo(opponent="OTH2", team_total=20.0),
        })
        assert week.vegas_multiplier("WR", "BUF", w, cfg) > 1.0

    def test_low_implied_total_lowers_offense(self):
        cfg = _spicy()
        w = week.WeeklyIntel(games={
            "BUF": week.GameInfo(opponent="MIA", team_total=14.0, opp_total=18.0),
            "OTH": week.GameInfo(opponent="OTH2", team_total=26.0),
        })
        assert week.vegas_multiplier("WR", "BUF", w, cfg) < 1.0

    def test_def_inverted_on_opponent_total(self):
        # A defense benefits when the OPPONENT is projected low, not when its
        # own offense is projected high.
        cfg = _spicy()
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=32.0, opp_total=10.0)})
        assert week.vegas_multiplier("DEF", "BUF", w, cfg) > 1.0

    def test_no_game_data_is_a_noop(self):
        cfg = _spicy()
        assert week.vegas_multiplier("WR", "BUF", week.WeeklyIntel(), cfg) == 1.0

    def test_zero_weight_is_a_noop(self):
        cfg = _spicy(vegas_weight=0.0)
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=45.0, opp_total=3.0)})
        assert week.vegas_multiplier("WR", "BUF", w, cfg) == 1.0

    def test_never_goes_below_floor(self):
        cfg = _spicy(vegas_weight=50.0)  # absurd weight, must still be clamped
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=1.0, opp_total=1.0)})
        assert week.vegas_multiplier("WR", "BUF", w, cfg) >= 0.5

    def test_uses_actual_slate_average_not_a_guess(self):
        # league_avg_total should reflect the real loaded games, not a fixed
        # constant, so the tilt is calibrated to an unusually high- or
        # low-scoring week rather than a generic league assumption.
        w = week.WeeklyIntel(games={
            "A": week.GameInfo(opponent="B", team_total=40.0),
            "C": week.GameInfo(opponent="D", team_total=40.0),
        })
        assert week.league_avg_total(w) == pytest.approx(40.0)


class TestSpiceIsOffByDefault:
    def test_stock_config_is_an_exact_noop(self):
        cfg = Config()  # every season weight defaults to 0.0
        roster = [_p("A", "WR", proj=20.0), _p("B", "RB", proj=10.0)]
        w = week.WeeklyIntel(
            players={"a": week.WeeklyPlayerIntel(name="A", upside=99.0, volatility=99.0)},
            games={"BUF": week.GameInfo(opponent="MIA", wind_mph=40.0, team_total=50.0)},
        )
        out = week.adjusted_players(roster, w, cfg.season, OUTDOOR)
        assert out[0].projected_points == pytest.approx(20.0)
        assert out[1].projected_points == pytest.approx(10.0)


class TestDecisionScale:
    def test_empty_roster_returns_the_floor(self):
        assert week.decision_scale([]) == week._MIN_DECISION_SCALE

    def test_flat_roster_returns_the_floor(self):
        roster = [_p(f"P{i}", "WR", proj=10.0) for i in range(10)]
        assert week.decision_scale(roster) == week._MIN_DECISION_SCALE

    def test_reflects_the_real_spread(self):
        roster = [_p(f"P{i}", "WR", proj=float(i * 10)) for i in range(10)]
        assert week.decision_scale(roster) > week._MIN_DECISION_SCALE


class TestSpiceLevelOneIsControl:
    """B5 -- level 1 of the re-derived two-axis SPICE_PRESETS must reduce
    `adjusted_players` to an exact no-op: every multiplier collapses to
    1.0 and `spice_bonus` to 0.0, so the agent lineup is bit-identical to
    the control (raw-projection) lineup. See
    tests/test_config.py::TestTwoAxisSpiceLadder for the field-level
    precondition this behavioral proof relies on.
    """

    def test_adjusted_players_is_bit_identical_to_input(self):
        cfg = SeasonConfig.from_spice_level(1)
        roster = [
            _p("A", "RB", proj=15.0), _p("B", "WR", proj=12.0), _p("C", "QB", proj=20.0),
        ]
        w = week.WeeklyIntel(
            games={"BUF": week.GameInfo(opponent="MIA", team_total=30.0, opp_total=10.0, wind_mph=40.0)},
            players={
                "a": week.WeeklyPlayerIntel(name="A", volatility=99.0, upside=99.0, usage_trend=99.0,
                                             momentum=99.0, divergence=99.0),
            },
        )
        out = week.adjusted_players(roster, w, cfg)
        for original, adjusted in zip(roster, out):
            assert adjusted.projected_points == pytest.approx(original.projected_points)

    def test_matches_a_bare_zeroed_config(self):
        # Level 1 and the dataclass's own all-zero default must agree --
        # there is no field level 1 sets that the bare default doesn't
        # already leave at 0.0, other than streaming_weight (K/DEF
        # streaming only, never touches adjusted_players).
        level_one = SeasonConfig.from_spice_level(1)
        bare = SeasonConfig()
        roster = [_p("A", "RB", proj=15.0)]
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=30.0, opp_total=10.0)})
        out_level_one = week.adjusted_players(roster, w, level_one)
        out_bare = week.adjusted_players(roster, w, bare)
        assert out_level_one[0].projected_points == pytest.approx(out_bare[0].projected_points)


class TestSpiceBonus:
    def test_boom_bust_player_gets_a_bonus_on_a_close_roster(self):
        cfg = _spicy()
        roster = [_p("Boom", "WR", proj=10.0), _p("Steady", "WR", proj=10.1)]
        w = week.WeeklyIntel(players={"boom": week.WeeklyPlayerIntel(name="Boom", volatility=90.0)})
        scale = week.decision_scale(roster)
        boom_bonus = week.spice_bonus(roster[0], w, cfg, scale)
        steady_bonus = week.spice_bonus(roster[1], w, cfg, scale)
        assert boom_bonus > steady_bonus

    def test_zero_weights_give_zero_bonus_regardless_of_intel(self):
        cfg = _spicy(volatility_weight=0.0, upside_lean_weight=0.0)
        p = _p("X", "WR")
        w = week.WeeklyIntel(players={"x": week.WeeklyPlayerIntel(name="X", volatility=99.0, upside=99.0)})
        assert week.spice_bonus(p, w, cfg, scale=100.0) == 0.0


class TestBuildWeekBrief:
    def _roster_positions(self):
        return {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "K": 1, "DEF": 1, "BN": 5}

    def _full_roster(self):
        return [
            _p("QB1", "QB", proj=20.0), _p("QB2", "QB", proj=15.0),
            _p("WR1", "WR", proj=18.0), _p("WR2", "WR", proj=16.0), _p("WR3", "WR", proj=8.0),
            _p("RB1", "RB", proj=17.0), _p("RB2", "RB", proj=14.0),
            _p("TE1", "TE", proj=9.0),
            _p("K1", "K", proj=8.0),
            _p("DEF1", "DEF", proj=7.0),
        ]

    def test_status_out_player_gets_benched_and_flagged(self):
        cfg = Config(roster_positions=self._roster_positions())
        roster = self._full_roster()
        roster[2] = _p("WR1", "WR", proj=18.0, status="O", selected_position="WR")  # currently starting, now OUT
        w = week.WeeklyIntel(players={"wr1": week.WeeklyPlayerIntel(name="WR1", status="O")})
        brief = week.build_week_brief(roster, cfg.roster_positions, week=3, cfg=cfg, weekly=w)
        assert any(m.from_slot == "WR" and m.to_slot == "BN" for m in brief.lineup.moves if m.player.name == "WR1")
        assert any("WR1" in a and "benched" in a for a in brief.alerts)

    def test_status_override_from_weekly_intel_alone_still_triggers_the_alert(self):
        # Regression case: the roster-source Player itself carries no status
        # at all (status="", the common case with no live Yahoo feed) --
        # only weekly/week-NN.yml says this player is OUT. The alert must
        # still fire; it previously checked the pre-override roster, which
        # never saw this override at all.
        cfg = Config(roster_positions=self._roster_positions())
        roster = self._full_roster()
        roster[2] = _p("WR1", "WR", proj=18.0, status="", selected_position="WR")
        w = week.WeeklyIntel(players={"wr1": week.WeeklyPlayerIntel(name="WR1", status="O")})
        brief = week.build_week_brief(roster, cfg.roster_positions, week=3, cfg=cfg, weekly=w)
        assert any("WR1" in a and "benched" in a for a in brief.alerts)

    def test_stale_intel_week_produces_an_alert(self):
        cfg = Config(roster_positions=self._roster_positions())
        w = week.WeeklyIntel(week=2)
        brief = week.build_week_brief(self._full_roster(), cfg.roster_positions, week=5, cfg=cfg, weekly=w)
        assert any("week 2" in a and "week 5" in a for a in brief.alerts)

    def test_matching_week_produces_no_stale_alert(self):
        cfg = Config(roster_positions=self._roster_positions())
        w = week.WeeklyIntel(week=5)
        brief = week.build_week_brief(self._full_roster(), cfg.roster_positions, week=5, cfg=cfg, weekly=w)
        assert not any("stale" in a or "probably" in a for a in brief.alerts)

    def test_notes_carry_through_to_the_brief(self):
        cfg = Config(roster_positions=self._roster_positions())
        w = week.WeeklyIntel(players={"rb1": week.WeeklyPlayerIntel(name="RB1", note="workload concern")})
        brief = week.build_week_brief(self._full_roster(), cfg.roster_positions, week=3, cfg=cfg, weekly=w)
        assert any(n.name == "RB1" and n.note == "workload concern" for n in brief.notes)

    def test_no_weekly_intel_still_produces_a_lineup(self):
        cfg = Config(roster_positions=self._roster_positions())
        brief = week.build_week_brief(self._full_roster(), cfg.roster_positions, week=3, cfg=cfg)
        assert brief.lineup.assignments


class TestRankStreamers:
    def _pool(self):
        return [
            mk_bp("Def A", "DEF", points=90.0, team="AAA"),
            mk_bp("Def B", "DEF", points=85.0, team="BBB"),
            mk_bp("Kicker A", "K", points=100.0, team="AAA"),
        ]

    def test_only_the_requested_position_is_returned(self):
        cfg = _spicy()
        out = week.rank_streamers(self._pool(), "DEF", week.WeeklyIntel(), cfg)
        assert all(c.position == "DEF" for c in out)
        assert len(out) == 2

    def test_favorable_matchup_promotes_a_lower_floor_option(self):
        cfg = _spicy(streaming_weight=1.0)  # pure matchup
        w = week.WeeklyIntel(games={
            "AAA": week.GameInfo(opponent="ZZZ", opp_total=8.0),   # great matchup for Def A
            "BBB": week.GameInfo(opponent="YYY", opp_total=35.0),  # brutal matchup for Def B
        })
        out = week.rank_streamers(self._pool(), "DEF", w, cfg)
        assert out[0].name == "Def A"

    def test_respects_limit(self):
        cfg = _spicy()
        out = week.rank_streamers(self._pool(), "DEF", week.WeeklyIntel(), cfg, limit=1)
        assert len(out) == 1

    def test_bye_this_week_is_excluded_outright(self):
        cfg = _spicy()
        pool = [
            mk_bp("Def A", "DEF", points=90.0, team="AAA", bye_week=5),
            mk_bp("Def B", "DEF", points=85.0, team="BBB", bye_week=9),
        ]
        out = week.rank_streamers(pool, "DEF", week.WeeklyIntel(), cfg, week=5)
        names = [c.name for c in out]
        assert "Def A" not in names
        assert "Def B" in names

    def test_no_week_argument_keeps_bye_players_in(self):
        cfg = _spicy()
        pool = [mk_bp("Def A", "DEF", points=90.0, team="AAA", bye_week=5)]
        out = week.rank_streamers(pool, "DEF", week.WeeklyIntel(), cfg)
        assert [c.name for c in out] == ["Def A"]

    def test_bye_on_a_different_week_is_unaffected(self):
        cfg = _spicy()
        pool = [mk_bp("Def A", "DEF", points=90.0, team="AAA", bye_week=5)]
        out = week.rank_streamers(pool, "DEF", week.WeeklyIntel(), cfg, week=6)
        assert [c.name for c in out] == ["Def A"]

    def test_momentum_promotes_a_lower_floor_option(self):
        # B5 -- momentum_weight now reaches rank_streamers via
        # _momentum_multiplier. Def B has a lower floor but a hot recent
        # scoring trend; a high enough momentum_weight should be able to
        # flip the ranking the same way a favorable matchup can.
        cfg = _spicy(streaming_weight=1.0, momentum_weight=2.0)
        w = week.WeeklyIntel(players={"def b": week.WeeklyPlayerIntel(name="Def B", momentum=100.0)})
        out = week.rank_streamers(self._pool(), "DEF", w, cfg)
        assert out[0].name == "Def B"

    def test_zero_momentum_weight_is_exact_noop(self):
        cfg = _spicy(momentum_weight=0.0)
        w = week.WeeklyIntel(players={"def a": week.WeeklyPlayerIntel(name="Def A", momentum=99.0)})
        with_entry = week.rank_streamers(self._pool(), "DEF", w, cfg)
        without_entry = week.rank_streamers(self._pool(), "DEF", week.WeeklyIntel(), cfg)
        assert [c.weekly_value for c in with_entry] == [c.weekly_value for c in without_entry]


class TestMomentumMultiplier:
    """B5 -- week._momentum_multiplier, the rank_streamers/waiver_candidates
    analog of spice_bonus's additive term (multiplicative here since those
    two callers have no per-decision decision_scale to anchor a fraction
    against)."""

    def test_none_entry_is_exact_noop(self):
        cfg = _spicy(momentum_weight=0.5, usage_weight=0.5, divergence_weight=0.5)
        assert week._momentum_multiplier(None, cfg) == 1.0

    def test_zero_weights_are_exact_noop_regardless_of_entry(self):
        cfg = _spicy(momentum_weight=0.0, usage_weight=0.0, divergence_weight=0.0)
        entry = week.WeeklyPlayerIntel(name="X", momentum=100.0, usage_trend=100.0, divergence=100.0)
        assert week._momentum_multiplier(entry, cfg) == 1.0

    def test_positive_weight_and_entry_gives_a_boost(self):
        cfg = _spicy(momentum_weight=0.3)
        entry = week.WeeklyPlayerIntel(name="X", momentum=80.0)
        assert week._momentum_multiplier(entry, cfg) > 1.0

    def test_floored_at_zero(self):
        cfg = _spicy(momentum_weight=-5.0)
        entry = week.WeeklyPlayerIntel(name="X", momentum=100.0)
        assert week._momentum_multiplier(entry, cfg) == 0.0


class TestWaiverCandidates:
    def _board(self, extra_bp=None, replacement=None):
        players = [
            mk_bp("Roster Rb", "RB", points=180.0, rank=1, vor=180.0),
            mk_bp("Roster Wr", "WR", points=170.0, rank=2, vor=170.0),
            mk_bp("Bench Scrub", "WR", points=40.0, rank=200, vor=40.0),
            mk_bp("Waiver Gem", "WR", points=200.0, rank=3, vor=200.0),
        ]
        if extra_bp:
            players.append(extra_bp)
        return Board(
            players=players, by_key={p.key: p for p in players},
            replacement=replacement or {}, starters_per_pos={}, tier_last={},
        )

    def _roster(self):
        # Weekly-scale numbers on purpose -- waiver_candidates' season-scale
        # comparison (via roster_board_keys/draft._season_score) must look
        # these up on the board's season totals before comparing, not use
        # these weekly numbers directly.
        return [
            _p("Roster Rb", "RB", proj=15.0),
            _p("Roster Wr", "WR", proj=14.0),
            _p("Bench Scrub", "WR", proj=3.0),
        ]

    def _layout(self):
        return {"RB": 1, "WR": 1, "BN": 3}

    def _cfg(self, **season_kw):
        # ros_blend=1.0 (pure rest-of-season) unless a test wants otherwise,
        # so these fixtures' weekly-scale numbers (irrelevant to what's being
        # tested here) don't have to be hand-crafted to match.
        season = dict(ros_blend=1.0)
        season.update(season_kw)
        return Config(roster_positions=self._layout(), season=SeasonConfig(**season))

    def test_a_real_upgrade_is_ranked(self):
        cfg = self._cfg()
        candidates, missing = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        assert missing == []
        names = [c.add_name for c in candidates]
        assert "Waiver Gem" in names

    def test_open_roster_spot_needs_no_drop(self):
        # capacity = 1 RB + 1 WR + 3 BN = 5; roster has 3 -- 2 spots open.
        cfg = self._cfg()
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        top = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert top.drop_name is None
        assert top.net == pytest.approx(top.value)  # no drop cost to subtract

    def test_full_roster_pairs_a_drop(self):
        # No bench slots at all -- roster (3 players) already exceeds the 2
        # starting slots, so adding anyone requires a real drop.
        layout = {"RB": 1, "WR": 1, "BN": 0}
        cfg = Config(roster_positions=layout, season=SeasonConfig(ros_blend=1.0))
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), layout, cfg, remaining_faab=100,
        )
        top = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert top.drop_name is not None
        assert top.net < top.value  # drop cost was actually subtracted

    def test_rostered_players_are_excluded_from_candidates(self):
        cfg = self._cfg()
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        assert "Roster Rb" not in [c.add_name for c in candidates]

    def test_unmatched_roster_player_is_reported_not_silently_dropped(self):
        cfg = self._cfg()
        roster = self._roster() + [_p("Waiver Pickup Not On Board", "WR", proj=5.0)]
        _, missing = week.waiver_candidates(
            roster, self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        assert "Waiver Pickup Not On Board" in missing

    def test_uses_season_scale_not_weekly_scale(self):
        # The regression case: Roster Wr's weekly proj (14.0) must NOT be what
        # gets compared against the board's season-scale numbers (170.0+), or
        # every waiver candidate looks like a massive upgrade over a real
        # starter purely from the unit mismatch.
        cfg = self._cfg()  # ros_blend=1.0 -- pure season-scale
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        # Waiver Gem (200) barely beats Roster Wr (170) in season terms --
        # the gain should be double digits, not the ~186-point gap a
        # weekly-vs-season mismatch (200 - 14) would produce.
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert gem.value < 50.0

    def test_replacement_subtraction_caps_gain_once_position_is_saturated(self):
        # Two WR starting slots, only one rostered -- the empty second slot
        # means a real replacement level actually would crack the lineup, so
        # subtracting it makes a visible difference (unlike the single-slot
        # fixtures elsewhere in this class, where the existing starter
        # already beats any plausible replacement and marginal_repl is 0
        # either way).
        layout = {"RB": 1, "WR": 2, "BN": 3}
        roster = [_p("Roster Rb", "RB", proj=15.0), _p("Roster Wr", "WR", proj=14.0)]
        board = self._board(replacement={"WR": 165.0})
        cfg = Config(roster_positions=layout, season=SeasonConfig(ros_blend=1.0))
        candidates, _ = week.waiver_candidates(roster, board, layout, cfg, remaining_faab=100)
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")

        board_no_repl = self._board()
        candidates_no_repl, _ = week.waiver_candidates(
            roster, board_no_repl, layout, cfg, remaining_faab=100,
        )
        gem_no_repl = next(c for c in candidates_no_repl if c.add_name == "Waiver Gem")
        assert gem.value < gem_no_repl.value

    def test_zero_remaining_faab_still_returns_candidates_with_zero_bid(self):
        cfg = self._cfg()
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=0,
        )
        assert candidates and all(c.max_bid == 0 for c in candidates)

    def test_ros_blend_endpoints(self):
        # ros_blend=1.0 is pure season-scale; ros_blend=0.0 is pure this-week
        # (candidate's weekly-equivalent must actually beat a starter to
        # register at all under a pure this-week evaluation).
        board = self._board()
        cfg_ros = self._cfg(ros_blend=1.0)
        candidates_ros, _ = week.waiver_candidates(
            self._roster(), board, cfg_ros.roster_positions, cfg_ros, remaining_faab=100,
        )
        gem_ros = next(c for c in candidates_ros if c.add_name == "Waiver Gem")

        cfg_week = self._cfg(ros_blend=0.0)
        candidates_week, _ = week.waiver_candidates(
            self._roster(), board, cfg_week.roster_positions, cfg_week,
            remaining_faab=100, weeks_remaining=17,
        )
        names_week = [c.add_name for c in candidates_week]
        # Waiver Gem's weekly-equivalent (200/17 ~= 11.8) does not beat the
        # rostered starter (Roster Wr, weekly proj 14.0) -- a pure this-week
        # evaluation correctly finds no gain, unlike the pure-ROS case.
        assert gem_ros.value > 0
        assert "Waiver Gem" not in names_week

    def test_rolling_priority_no_bid_and_claim_note_set(self):
        board = self._board()
        cfg = Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 0},  # force a drop -> real claim_cost
            season=SeasonConfig(ros_blend=1.0, priority_value=0.5),
            league_file="",
        )
        from ffbot.config import LeagueScoring
        cfg.league = LeagueScoring(waiver_type="rolling")
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg, my_priority=1,
        )
        top = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert top.max_bid == 0
        assert top.claim_note != ""
        assert "priority" in top.claim_note.lower() or "HOLD" in top.claim_note

    def test_rolling_priority_cheap_at_bottom_of_list(self):
        board = self._board()
        layout = {"RB": 1, "WR": 1, "BN": 0}
        from ffbot.config import LeagueScoring
        cfg_best = Config(roster_positions=layout, season=SeasonConfig(ros_blend=1.0, priority_value=0.9))
        cfg_best.league = LeagueScoring(waiver_type="rolling")
        cfg_worst = Config(roster_positions=layout, season=SeasonConfig(ros_blend=1.0, priority_value=0.9))
        cfg_worst.league = LeagueScoring(waiver_type="rolling")

        best_priority, _ = week.waiver_candidates(
            self._roster(), board, layout, cfg_best, my_priority=1,
        )
        worst_priority, _ = week.waiver_candidates(
            self._roster(), board, layout, cfg_worst, my_priority=12,
        )
        gem_best = next(c for c in best_priority if c.add_name == "Waiver Gem")
        gem_worst = next(c for c in worst_priority if c.add_name == "Waiver Gem")
        # Same gain, same drop -- spending priority 1 costs strictly more
        # than spending priority 12 (nearly free).
        assert gem_worst.net > gem_best.net

    def test_bye_this_week_zeroes_the_week_gain_component(self):
        # Pure this-week (ros_blend=0.0) so a bye's zeroed week_gain is the
        # entire story -- Waiver Gem's weekly-equivalent otherwise beats the
        # rostered starter (built for test_ros_blend_endpoints' opposite
        # case: a *short* season here so the weekly-equivalent is large
        # enough to clear the bar in the first place).
        board = self._board(extra_bp=None)
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        cfg = self._cfg(ros_blend=0.0)

        candidates_playing, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=3, week=6,
        )
        candidates_bye, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=3, week=5,
        )
        gem_playing = next(c for c in candidates_playing if c.add_name == "Waiver Gem")
        names_bye = [c.add_name for c in candidates_bye]
        assert gem_playing.value > 0
        # On bye, week_gain is exactly 0 -- with no ROS component at all
        # (ros_blend=0.0) the candidate's total gain is <= 0 and drops out.
        assert "Waiver Gem" not in names_bye

    def test_bye_this_week_noted_in_reason_when_still_ranked(self):
        # A middling ros_blend keeps the candidate ranked (the ROS half of
        # the blend is untouched by a one-week bye) while still zeroing the
        # week_gain half -- reason should say why.
        board = self._board()
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        cfg = self._cfg(ros_blend=0.5)
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17, week=5,
        )
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert "on bye this week" in gem.reason

    def test_bye_on_a_different_week_is_unaffected(self):
        board = self._board()
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        cfg = self._cfg(ros_blend=1.0)
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg, remaining_faab=100, week=6,
        )
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert "on bye this week" not in gem.reason

    def test_no_week_argument_is_unaffected_by_bye(self):
        board = self._board()
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        cfg = self._cfg(ros_blend=1.0)
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg, remaining_faab=100,
        )
        assert "Waiver Gem" in [c.add_name for c in candidates]

    def test_default_weekly_points_none_is_exact_noop(self):
        cfg = self._cfg(ros_blend=0.0)
        without, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17,
        )
        explicit_none, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17, weekly_points=None,
        )
        # Neither run clears the bar under a pure this-week evaluation (see
        # test_ros_blend_endpoints) -- confirming weekly_points=None changes
        # nothing about which candidates even survive.
        assert [c.add_name for c in without] == [c.add_name for c in explicit_none] == []

    def test_weekly_points_overrides_the_board_rescaled_estimate(self):
        # Same setup as test_ros_blend_endpoints (pure this-week, where the
        # board-rescaled estimate for Waiver Gem does NOT beat the rostered
        # starter) -- but now a real weekly number is supplied that DOES.
        cfg = self._cfg(ros_blend=0.0)
        board = self._board()
        gem_key = next(bp.key for bp in board.players if bp.name == "Waiver Gem")
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17,
            weekly_points={gem_key: 25.0},  # real number, well above 200/17 ~= 11.8
        )
        names = [c.add_name for c in candidates]
        assert "Waiver Gem" in names

    def test_candidate_missing_from_weekly_points_falls_back_to_board_rescale(self):
        cfg = self._cfg(ros_blend=0.0)
        board = self._board()
        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17,
            weekly_points={"some other player:WR": 999.0},  # doesn't cover Waiver Gem
        )
        # Falls back to the old board-rescaled estimate, which does not
        # clear the bar -- identical outcome to weekly_points=None.
        assert "Waiver Gem" not in [c.add_name for c in candidates]

    def test_weekly_points_does_not_affect_the_ros_half(self):
        # A middling blend so both halves matter; ros_gain must stay
        # entirely board-derived regardless of what weekly_points says.
        cfg = self._cfg(ros_blend=1.0)
        board = self._board()
        gem_key = next(bp.key for bp in board.players if bp.name == "Waiver Gem")

        without, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg, remaining_faab=100,
        )
        with_override, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weekly_points={gem_key: -9999.0},
        )
        gem_without = next(c for c in without if c.add_name == "Waiver Gem")
        gem_with = next(c for c in with_override if c.add_name == "Waiver Gem")
        # Pure ROS (ros_blend=1.0) -- a wildly different weekly_points value
        # must not move the result at all.
        assert gem_without.value == pytest.approx(gem_with.value)

    def test_bye_still_zeroes_the_week_component_even_with_a_real_weekly_number(self):
        cfg = self._cfg(ros_blend=0.0)
        board = self._board()
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        gem_key = board.players[-1].key

        candidates, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17, week=5,
            weekly_points={gem_key: 999.0},  # would easily clear the bar if not zeroed
        )
        assert "Waiver Gem" not in [c.add_name for c in candidates]

    def test_default_weekly_none_is_exact_noop(self):
        # B5 -- `weekly` is a new optional param; the default must reproduce
        # every prior call site's behavior bit-identically.
        cfg = self._cfg(ros_blend=0.5, momentum_weight=0.4)
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")
        candidates_explicit, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100, weekly=None,
        )
        gem_explicit = next(c for c in candidates_explicit if c.add_name == "Waiver Gem")
        assert gem.value == pytest.approx(gem_explicit.value)

    def test_momentum_moves_this_week_half_only(self):
        # ros_blend=1.0 (pure rest-of-season) -- momentum must have ZERO
        # effect here, since it only ever touches the this-week half of the
        # blend. This is the direct proof of the "never the ROS half"
        # invariant week.waiver_candidates's docstring promises.
        cfg = self._cfg(ros_blend=1.0, momentum_weight=2.0)
        w = week.WeeklyIntel(players={"waiver gem": week.WeeklyPlayerIntel(name="Waiver Gem", momentum=100.0)})
        with_momentum, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100, weekly=w,
        )
        without_momentum, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        gem_with = next(c for c in with_momentum if c.add_name == "Waiver Gem")
        gem_without = next(c for c in without_momentum if c.add_name == "Waiver Gem")
        assert gem_with.value == pytest.approx(gem_without.value)

    def test_momentum_moves_the_blend_when_this_week_matters(self):
        # ros_blend=0.5 -- the this-week half of the blend is now in play
        # (ros_blend=1.0 above proved momentum is a no-op there), so a
        # strong momentum entry on the candidate should visibly raise its
        # gain over the same candidate with no momentum entry.
        cfg = self._cfg(ros_blend=0.5, momentum_weight=2.0)
        w = week.WeeklyIntel(players={"waiver gem": week.WeeklyPlayerIntel(name="Waiver Gem", momentum=100.0)})
        with_momentum, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100, weekly=w,
        )
        without_momentum, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, remaining_faab=100,
        )
        gem_with = next(c for c in with_momentum if c.add_name == "Waiver Gem")
        gem_without = next(c for c in without_momentum if c.add_name == "Waiver Gem")
        assert gem_with.value > gem_without.value

    def test_bye_week_candidate_is_untouched_by_momentum(self):
        # A candidate zeroed out for being on bye must stay exactly the
        # same gain with or without a momentum entry -- momentum must never
        # revive a phantom this-week contribution for a bye-week player.
        # ros_blend=0.5 keeps the ROS half contributing so the candidate
        # doesn't just get filtered out by gain<=0 regardless of momentum.
        board = self._board()
        board.players[-1] = dataclasses.replace(board.players[-1], bye_week=5)
        board.by_key[board.players[-1].key] = board.players[-1]
        cfg = self._cfg(ros_blend=0.5, momentum_weight=2.0)
        w = week.WeeklyIntel(players={"waiver gem": week.WeeklyPlayerIntel(name="Waiver Gem", momentum=100.0)})
        with_momentum, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17, week=5, weekly=w,
        )
        without_momentum, _ = week.waiver_candidates(
            self._roster(), board, cfg.roster_positions, cfg,
            remaining_faab=100, weeks_remaining=17, week=5,
        )
        gem_with = next(c for c in with_momentum if c.add_name == "Waiver Gem")
        gem_without = next(c for c in without_momentum if c.add_name == "Waiver Gem")
        assert gem_with.value == pytest.approx(gem_without.value)


class TestDenialUrgencyInWaivers:
    def _board(self):
        players = [
            mk_bp("My Rb", "RB", points=100.0, vor=50.0),
            mk_bp("Contested Wr", "WR", points=150.0, vor=100.0),
        ]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def _roster(self):
        return [_p("My Rb", "RB", proj=10.0)]

    def _cfg(self, denial_weight):
        cfg = Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 2},
            draft=DraftConfig(num_teams=12),
            season=SeasonConfig(ros_blend=1.0, denial_weight=denial_weight),
        )
        cfg.league = LeagueScoring(playoff_teams=4, teams=[TeamStanding(name="Rival", seed=4)])
        return cfg

    def test_urgency_lifts_net_when_denial_weight_set(self):
        board = self._board()
        # Rival has no WR at all -- Contested Wr is a real gain for them.
        rosters = LeagueRosters(teams={"Rival": []})

        off = week.waiver_candidates(
            self._roster(), board, self._cfg(0.0).roster_positions, self._cfg(0.0), league_rosters=rosters,
        )[0]
        on = week.waiver_candidates(
            self._roster(), board, self._cfg(1.0).roster_positions, self._cfg(1.0), league_rosters=rosters,
        )[0]
        wr_off = next(c for c in off if c.add_name == "Contested Wr")
        wr_on = next(c for c in on if c.add_name == "Contested Wr")
        assert wr_on.net > wr_off.net
        assert "claim urgency" in wr_on.reason

    def test_no_league_rosters_is_a_noop(self):
        cfg = self._cfg(1.0)
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, cfg, league_rosters=None,
        )
        wr = next(c for c in candidates if c.add_name == "Contested Wr")
        assert "claim urgency" not in wr.reason


class TestIrStashCandidates:
    def _board(self):
        players = [
            mk_bp("Rostered", "RB", points=180.0),
            mk_bp("Hurt Stud", "WR", points=150.0),
            mk_bp("Healthy Fa", "WR", points=100.0),
        ]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def _layout(self):
        return {"RB": 1, "WR": 1, "BN": 1, "IR": 1}

    def _roster(self):
        return [_p("Rostered", "RB", proj=15.0)]

    def test_ir_eligible_free_agent_surfaced(self):
        board = self._board()
        cfg = Config(roster_positions=self._layout())
        weekly = week.WeeklyIntel(players={
            "hurt stud": week.WeeklyPlayerIntel(name="Hurt Stud", status="IR"),
        })
        out = week.ir_stash_candidates(self._roster(), board, cfg.roster_positions, weekly, cfg)
        assert [c.add_name for c in out] == ["Hurt Stud"]
        assert out[0].value == 150.0

    def test_healthy_free_agent_not_included(self):
        board = self._board()
        cfg = Config(roster_positions=self._layout())
        weekly = week.WeeklyIntel(players={
            "healthy fa": week.WeeklyPlayerIntel(name="Healthy Fa", status=""),
        })
        out = week.ir_stash_candidates(self._roster(), board, cfg.roster_positions, weekly, cfg)
        assert out == []

    def test_no_open_ir_slot_returns_nothing(self):
        board = self._board()
        cfg = Config(roster_positions=self._layout())
        weekly = week.WeeklyIntel(players={
            "hurt stud": week.WeeklyPlayerIntel(name="Hurt Stud", status="IR"),
        })
        roster = self._roster() + [_p("Already Parked", "WR", proj=1.0, selected_position="IR", status="IR")]
        out = week.ir_stash_candidates(roster, board, cfg.roster_positions, weekly, cfg)
        assert out == []

    def test_rostered_player_excluded_even_if_flagged(self):
        board = self._board()
        cfg = Config(roster_positions=self._layout())
        weekly = week.WeeklyIntel(players={
            "rostered": week.WeeklyPlayerIntel(name="Rostered", status="IR"),
        })
        out = week.ir_stash_candidates(self._roster(), board, cfg.roster_positions, weekly, cfg)
        assert out == []


class TestRosterSpace:
    def _layout(self):
        return {"QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 5, "IR": 1}

    def test_capacity_is_starters_plus_bench_ir_excluded(self):
        space = week.roster_space([], self._layout())
        assert space.capacity == 14  # 9 starters + 5 bench

    def test_open_spots_and_must_drop(self):
        layout = {"QB": 1, "BN": 2}  # capacity 3
        roster = [_p("A", "QB"), _p("B", "QB")]
        space = week.roster_space(roster, layout)
        assert space.occupied == 2
        assert space.open_spots == 1
        assert space.must_drop == 0

    def test_full_roster_requires_a_drop(self):
        layout = {"QB": 1, "BN": 2}  # capacity 3
        roster = [_p("A", "QB"), _p("B", "QB"), _p("C", "QB")]
        space = week.roster_space(roster, layout)
        assert space.open_spots == 0
        assert space.must_drop == 1

    def test_ir_parked_players_do_not_count_against_capacity(self):
        layout = {"QB": 1, "BN": 1, "IR": 1}  # capacity 2 (IR excluded)
        roster = [
            _p("Starter", "QB"),
            _p("Bench", "QB"),
            _p("Hurt", "QB", selected_position="IR", status="IR"),
        ]
        space = week.roster_space(roster, layout)
        assert space.ir_parked == 1
        assert space.occupied == 2  # Hurt not counted
        assert space.open_spots == 0  # full otherwise


class TestStreamingBaseline:
    def test_zero_when_current_starter_already_beats_replacement(self):
        # A strong rostered starter already occupies the only slot, so
        # replacement-level can never crack the lineup -- the option value
        # of a stream at this position is genuinely zero right now.
        board = Board(
            players=[mk_bp("Star", "RB", points=200.0, vor=150.0)],
            by_key={}, replacement={"RB": 50.0}, starters_per_pos={}, tier_last={},
        )
        board.by_key = {p.key: p for p in board.players}
        cfg = Config(roster_positions={"RB": 1, "BN": 1})
        keys = ["star:RB"]
        assert week.streaming_baseline("RB", keys, board, cfg) == 0.0

    def test_positive_when_the_position_has_a_real_hole(self):
        # Nobody rostered at RB at all -- replacement level would be a real
        # upgrade over the empty slot.
        board = Board(
            players=[mk_bp("Someone Else", "WR", points=100.0, vor=50.0)],
            by_key={}, replacement={"RB": 50.0, "WR": 50.0}, starters_per_pos={}, tier_last={},
        )
        board.by_key = {p.key: p for p in board.players}
        cfg = Config(roster_positions={"RB": 1, "WR": 1, "BN": 1})
        keys = ["someone else:WR"]
        assert week.streaming_baseline("RB", keys, board, cfg) == pytest.approx(50.0)

    def test_zero_for_unknown_position(self):
        board = Board(players=[], by_key={}, replacement={}, starters_per_pos={}, tier_last={})
        cfg = Config()
        assert week.streaming_baseline("QB", [], board, cfg) == 0.0


class TestDropCostAndHoldMargin:
    def _board(self):
        players = [
            mk_bp("Rb1", "RB", points=200.0, vor=150.0),
            mk_bp("Wr1", "WR", points=190.0, vor=145.0),
            mk_bp("Good Backup", "RB", points=90.0, vor=40.0),  # above replacement
            mk_bp("Bad Backup", "WR", points=20.0, vor=-25.0),  # below replacement
        ]
        return Board(
            players=players, by_key={p.key: p for p in players},
            replacement={"RB": 50.0, "WR": 45.0}, starters_per_pos={}, tier_last={},
        )

    def _cfg(self):
        return Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 2},
            draft=DraftConfig(depth_weight=0.5, depth_decay=1.0),
        )

    def test_starter_has_a_large_positive_drop_cost(self):
        board, cfg = self._board(), self._cfg()
        keys = [p.key for p in board.players]
        assert week.drop_cost("rb1:RB", keys, board, cfg) > 100.0

    def test_above_replacement_backup_has_positive_drop_cost(self):
        board, cfg = self._board(), self._cfg()
        keys = [p.key for p in board.players]
        # No starting-lineup delta (he's on the bench either way) -- the
        # whole positive number here is the VOR-based depth term.
        assert week.drop_cost("good backup:RB", keys, board, cfg) == pytest.approx(0.5 * 40.0)

    def test_below_replacement_backup_has_zero_drop_cost(self):
        board, cfg = self._board(), self._cfg()
        keys = [p.key for p in board.players]
        assert week.drop_cost("bad backup:WR", keys, board, cfg) == 0.0

    def test_hold_margin_orders_backups_by_value(self):
        board, cfg = self._board(), self._cfg()
        keys = [p.key for p in board.players]
        good = week.hold_margin("good backup:RB", keys, board, cfg)
        bad = week.hold_margin("bad backup:WR", keys, board, cfg)
        assert good > bad

    def test_blocking_bonus_only_applies_when_flagged(self):
        board = self._board()
        cfg = Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 2},
            draft=DraftConfig(depth_weight=0.5, depth_decay=1.0),
            season=SeasonConfig(blocking_hold_bonus=25.0),
        )
        keys = [p.key for p in board.players]
        without_bonus = week.hold_margin("bad backup:WR", keys, board, cfg, is_blocking=False)
        with_bonus = week.hold_margin("bad backup:WR", keys, board, cfg, is_blocking=True)
        assert with_bonus == pytest.approx(without_bonus + 25.0)


class TestClassifyRoster:
    def _board(self):
        players = [
            mk_bp("Rb1", "RB", points=200.0, vor=150.0),
            mk_bp("Wr1", "WR", points=190.0, vor=145.0),
            mk_bp("Good Backup", "RB", points=90.0, vor=40.0),
            mk_bp("Bad Backup", "WR", points=20.0, vor=-25.0),
        ]
        return Board(
            players=players, by_key={p.key: p for p in players},
            replacement={"RB": 50.0, "WR": 45.0}, starters_per_pos={}, tier_last={},
        )

    def _cfg(self, **season_kw):
        return Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 2},
            draft=DraftConfig(depth_weight=0.5, depth_decay=1.0),
            season=SeasonConfig(**season_kw),
        )

    def _roster(self):
        return [_p("Rb1", "RB"), _p("Wr1", "WR"), _p("Good Backup", "RB"), _p("Bad Backup", "WR")]

    def test_starters_are_always_core(self):
        classes, missing = week.classify_roster(self._roster(), self._board(), self._cfg())
        assert missing == []
        by_name = {c.name: c for c in classes}
        assert by_name["Rb1"].classification == "CORE"
        assert by_name["Wr1"].classification == "CORE"

    def test_above_replacement_backup_is_core_below_replacement_is_stream(self):
        classes, _ = week.classify_roster(self._roster(), self._board(), self._cfg())
        by_name = {c.name: c for c in classes}
        assert by_name["Good Backup"].classification == "CORE"
        assert by_name["Bad Backup"].classification == "STREAM"

    def test_derived_not_hardcoded_min_stream_spots_defaults_to_no_floor(self):
        # With min_stream_spots at its 0.0 default, a roster with zero
        # STREAM-classified players is a legitimate, unforced outcome.
        cfg = self._cfg()
        assert cfg.season.min_stream_spots == 0

    def test_min_stream_spots_floor_demotes_weakest_core(self):
        cfg = self._cfg(min_stream_spots=2)
        classes, _ = week.classify_roster(self._roster(), self._board(), cfg)
        stream_count = sum(1 for c in classes if c.classification == "STREAM")
        assert stream_count >= 2
        # The demoted player must be the weakest-margin CORE, not an
        # arbitrary one -- Good Backup has the smallest positive margin of
        # the two starters+good-backup CORE group.
        by_name = {c.name: c for c in classes}
        assert by_name["Good Backup"].classification == "STREAM"

    def test_missing_roster_player_surfaced_not_silently_dropped(self):
        roster = self._roster() + [_p("Not On Board", "WR")]
        classes, missing = week.classify_roster(roster, self._board(), self._cfg())
        assert "Not On Board" in missing
        assert "Not On Board" not in [c.name for c in classes]


class TestBuildRosterStatus:
    def test_end_to_end(self):
        board = Board(
            players=[
                mk_bp("Rb1", "RB", points=200.0, vor=150.0),
                mk_bp("Wr1", "WR", points=190.0, vor=145.0),
            ],
            by_key={}, replacement={"RB": 50.0, "WR": 45.0}, starters_per_pos={}, tier_last={},
        )
        board.by_key = {p.key: p for p in board.players}
        cfg = Config(roster_positions={"RB": 1, "WR": 1, "BN": 1})
        roster = [_p("Rb1", "RB"), _p("Wr1", "WR")]
        status = week.build_roster_status(roster, cfg.roster_positions, board, cfg)
        assert status.space.capacity == 3
        assert status.space.occupied == 2
        assert len(status.core) == 2
        assert status.missing == []


class TestDefenseTeamResolution:
    """DEF entries routinely carry a blank or full-city-name team field
    rather than a clean abbreviation -- weekly.games and data/stadiums.yml
    are both keyed by abbreviation, so without resolving through
    names.defense_key, a defense's matchup silently fails to be found even
    when it was genuinely researched. Caught by hand running the full
    pipeline end to end before this fix existed.
    """

    def test_full_city_name_resolves_to_abbreviation(self):
        assert week._resolve_team("DEF", "", "Baltimore Ravens") == "BAL"

    def test_blank_team_with_dst_suffix_resolves(self):
        assert week._resolve_team("DEF", "", "Ravens D/ST") == "BAL"

    def test_already_clean_abbreviation_passes_through(self):
        assert week._resolve_team("DEF", "BAL", "Baltimore Ravens") == "BAL"

    def test_non_defense_positions_are_never_touched(self):
        # A WR named e.g. "Buffalo" (hypothetically) must not get
        # reinterpreted as a team -- this path is DEF-only.
        assert week._resolve_team("WR", "BUF", "Some Guy") == "BUF"

    def test_streaming_finds_the_researched_matchup(self):
        cfg = _spicy(streaming_weight=1.0)
        pool = [mk_bp("Baltimore Ravens", "DEF", points=118.0, team="")]
        w = week.WeeklyIntel(games={"BAL": week.GameInfo(opponent="CIN", opp_total=14.0)})
        out = week.rank_streamers(pool, "DEF", w, cfg)
        assert "CIN" in out[0].reason

    def test_adjusted_players_applies_vegas_to_an_unabbreviated_defense(self):
        cfg = Config(season=SeasonConfig(vegas_weight=0.5))
        roster = [_p("Baltimore Ravens", "DEF", proj=10.0, team="")]
        w = week.WeeklyIntel(games={
            "BAL": week.GameInfo(opponent="CIN", opp_total=10.0),
            "OTH": week.GameInfo(opponent="OTH2", opp_total=25.0),
        })
        out = week.adjusted_players(roster, w, cfg.season, OUTDOOR)
        assert out[0].projected_points > 10.0


class TestGameScriptMultiplier:
    def test_zero_weight_is_exact_noop(self):
        cfg = SeasonConfig(game_script_weight=0.0)
        game = week.GameInfo(opponent="MIA", team_total=30.0, opp_total=10.0)
        assert week.game_script_multiplier("RB", "BUF", game, cfg) == 1.0

    def test_missing_totals_is_noop(self):
        cfg = SeasonConfig(game_script_weight=0.5)
        game = week.GameInfo(opponent="MIA")
        assert week.game_script_multiplier("RB", "BUF", game, cfg) == 1.0

    def test_missing_game_is_noop(self):
        cfg = SeasonConfig(game_script_weight=0.5)
        assert week.game_script_multiplier("RB", "BUF", None, cfg) == 1.0

    def test_kicker_is_always_noop_regardless_of_weight(self):
        cfg = SeasonConfig(game_script_weight=0.9)
        game = week.GameInfo(opponent="MIA", team_total=40.0, opp_total=3.0)
        assert week.game_script_multiplier("K", "BUF", game, cfg) == 1.0

    def test_favored_rb_gets_boosted(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        game = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)  # +10 margin
        assert week.game_script_multiplier("RB", "BUF", game, cfg) > 1.0

    def test_favored_wr_gets_discounted(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        game = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        assert week.game_script_multiplier("WR", "BUF", game, cfg) < 1.0

    def test_underdog_wr_gets_boosted(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        game = week.GameInfo(opponent="MIA", team_total=15.0, opp_total=25.0)  # -10 margin
        assert week.game_script_multiplier("WR", "BUF", game, cfg) > 1.0

    def test_favored_def_gets_boosted(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        game = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        assert week.game_script_multiplier("DEF", "BUF", game, cfg) > 1.0

    def test_margin_beyond_scale_does_not_diverge_further(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        at_scale = week.GameInfo(opponent="MIA", team_total=30.0, opp_total=20.0)  # +10
        beyond = week.GameInfo(opponent="MIA", team_total=50.0, opp_total=10.0)  # +40
        assert week.game_script_multiplier("RB", "BUF", at_scale, cfg) == pytest.approx(
            week.game_script_multiplier("RB", "BUF", beyond, cfg)
        )

    def test_wired_into_adjusted_players(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        roster = [_p("Favored Rb", "RB", proj=10.0)]
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)})
        out = week.adjusted_players(roster, w, cfg)
        assert out[0].projected_points > 10.0

    def test_stock_config_leaves_adjusted_players_bit_identical(self):
        cfg = Config()  # every season weight, including game_script_weight, defaults to 0.0
        roster = [_p("A", "RB", proj=20.0)]
        w = week.WeeklyIntel(games={"BUF": week.GameInfo(opponent="MIA", team_total=40.0, opp_total=3.0)})
        out = week.adjusted_players(roster, w, cfg.season)
        assert out[0].projected_points == pytest.approx(20.0)


class TestGameScriptFavoriteUnderdogScale:
    """B5 -- game_script_favorite_scale/underdog_scale, added to make "drop
    the pass-catcher discount, keep only the RB lean" a config sweep. See
    game_script_weight's docstring in ffbot/config.py for the retirement
    verdict; these two dials survive it since they cost nothing and are
    what made the sweep possible at all.
    """

    def test_default_scales_reproduce_original_behavior_bit_identically(self):
        cfg_default = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0)
        cfg_explicit = SeasonConfig(
            game_script_weight=0.5, game_script_scale=10.0,
            game_script_favorite_scale=1.0, game_script_underdog_scale=1.0,
        )
        favored = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        for pos in ("RB", "QB", "WR", "TE", "DEF"):
            assert week.game_script_multiplier(pos, "BUF", favored, cfg_default) == pytest.approx(
                week.game_script_multiplier(pos, "BUF", favored, cfg_explicit)
            )

    def test_underdog_scale_zero_removes_pass_catcher_discount_only(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0, game_script_underdog_scale=0.0)
        favored = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        # WR/QB/TE (negative leans) fully zeroed -> exact no-op.
        assert week.game_script_multiplier("WR", "BUF", favored, cfg) == 1.0
        assert week.game_script_multiplier("QB", "BUF", favored, cfg) == 1.0
        assert week.game_script_multiplier("TE", "BUF", favored, cfg) == 1.0
        # RB/DEF (positive leans) untouched -- favorite_scale still 1.0.
        assert week.game_script_multiplier("RB", "BUF", favored, cfg) > 1.0
        assert week.game_script_multiplier("DEF", "BUF", favored, cfg) > 1.0

    def test_favorite_scale_zero_removes_rb_def_lean_only(self):
        cfg = SeasonConfig(game_script_weight=0.5, game_script_scale=10.0, game_script_favorite_scale=0.0)
        favored = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        assert week.game_script_multiplier("RB", "BUF", favored, cfg) == 1.0
        assert week.game_script_multiplier("DEF", "BUF", favored, cfg) == 1.0
        assert week.game_script_multiplier("WR", "BUF", favored, cfg) < 1.0

    def test_both_scales_zero_is_exact_noop_regardless_of_weight(self):
        favored = week.GameInfo(opponent="MIA", team_total=25.0, opp_total=15.0)
        for weight in (0.15, 0.30, 0.90):
            cfg = SeasonConfig(
                game_script_weight=weight, game_script_favorite_scale=0.0, game_script_underdog_scale=0.0,
            )
            for pos in ("RB", "QB", "WR", "TE", "DEF"):
                assert week.game_script_multiplier(pos, "BUF", favored, cfg) == 1.0


class TestSameGameConflicts:
    def _roster_positions(self):
        return {"QB": 1, "WR": 1, "DEF": 1, "BN": 5}

    def test_starting_def_against_own_qb_warns(self):
        roster = [
            _p("My Qb", "QB", proj=20.0, team="BUF"),
            _p("My Wr", "WR", proj=15.0, team="MIA"),
            _p("My Def", "DEF", proj=10.0, team="MIA"),  # MIA's DEF plays BUF (opponent="BUF")
        ]
        w = week.WeeklyIntel(games={"MIA": week.GameInfo(opponent="BUF")})
        plan = week.optimize(roster, self._roster_positions(), None, Config(roster_positions=self._roster_positions()))
        out = week.same_game_conflicts(plan, w)
        assert any("My Def" in msg and "My Qb" in msg for msg in out)

    def test_no_conflict_when_different_games(self):
        roster = [
            _p("My Qb", "QB", proj=20.0, team="BUF"),
            _p("My Def", "DEF", proj=10.0, team="MIA"),
        ]
        w = week.WeeklyIntel(games={"MIA": week.GameInfo(opponent="NE")})
        plan = week.optimize(roster, self._roster_positions(), None, Config(roster_positions=self._roster_positions()))
        out = week.same_game_conflicts(plan, w)
        assert out == []

    def test_no_researched_game_produces_no_conflict(self):
        roster = [
            _p("My Qb", "QB", proj=20.0, team="BUF"),
            _p("My Def", "DEF", proj=10.0, team="MIA"),
        ]
        plan = week.optimize(roster, self._roster_positions(), None, Config(roster_positions=self._roster_positions()))
        out = week.same_game_conflicts(plan, week.WeeklyIntel())
        assert out == []


class TestMatchupLean:
    def test_equal_totals_is_zero(self):
        assert week.matchup_lean(100.0, 100.0) == 0.0

    def test_both_nonpositive_is_zero(self):
        assert week.matchup_lean(0.0, 0.0) == 0.0
        assert week.matchup_lean(-5.0, -3.0) == 0.0

    def test_stronger_roster_is_positive(self):
        assert week.matchup_lean(150.0, 100.0) > 0.0

    def test_weaker_roster_is_negative(self):
        assert week.matchup_lean(100.0, 150.0) < 0.0

    def test_clamped_to_unit_range(self):
        lean = week.matchup_lean(1000.0, 1.0)
        assert -1.0 <= lean <= 1.0


class TestVarianceMultiplier:
    def test_zero_weight_is_exact_noop(self):
        cfg = SeasonConfig(matchup_variance_weight=0.0)
        assert week._variance_multiplier(-1.0, cfg) == 1.0
        assert week._variance_multiplier(1.0, cfg) == 1.0

    def test_unknown_lean_is_exact_noop(self):
        cfg = SeasonConfig(matchup_variance_weight=0.9)
        assert week._variance_multiplier(0.0, cfg) == 1.0

    def test_underdog_amplifies(self):
        cfg = SeasonConfig(matchup_variance_weight=0.5)
        assert week._variance_multiplier(-1.0, cfg) > 1.0

    def test_favorite_dampens(self):
        cfg = SeasonConfig(matchup_variance_weight=0.5)
        assert week._variance_multiplier(1.0, cfg) < 1.0

    def test_never_goes_negative(self):
        cfg = SeasonConfig(matchup_variance_weight=5.0)
        assert week._variance_multiplier(1.0, cfg) == 0.0


class TestSpiceBonusMatchupConditioning:
    def test_underdog_amplifies_volatility_bonus(self):
        cfg = _spicy(matchup_variance_weight=0.5)
        p = _p("Boom", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"boom": week.WeeklyPlayerIntel(name="Boom", volatility=90.0)})
        favored = week.spice_bonus(p, w, cfg, scale=100.0, lean=1.0)
        underdog = week.spice_bonus(p, w, cfg, scale=100.0, lean=-1.0)
        assert underdog > favored

    def test_usage_term_is_not_lean_scaled(self):
        cfg = _spicy(usage_weight=0.3, volatility_weight=0.0, upside_lean_weight=0.0, matchup_variance_weight=0.9)
        p = _p("Volume", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"volume": week.WeeklyPlayerIntel(name="Volume", usage_trend=80.0)})
        favored = week.spice_bonus(p, w, cfg, scale=100.0, lean=1.0)
        underdog = week.spice_bonus(p, w, cfg, scale=100.0, lean=-1.0)
        assert favored == pytest.approx(underdog)

    def test_default_lean_is_zero_and_noop_on_multiplier(self):
        cfg = _spicy()
        p = _p("Boom", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"boom": week.WeeklyPlayerIntel(name="Boom", volatility=90.0)})
        no_lean_arg = week.spice_bonus(p, w, cfg, scale=100.0)
        explicit_zero = week.spice_bonus(p, w, cfg, scale=100.0, lean=0.0)
        assert no_lean_arg == pytest.approx(explicit_zero)


class TestUsageScore:
    def test_none_entry_is_zero(self):
        assert week.usage_score(None) == 0.0

    def test_no_usage_trend_is_zero(self):
        entry = week.WeeklyPlayerIntel(name="X")
        assert week.usage_score(entry) == 0.0

    def test_scales_to_unit_range(self):
        entry = week.WeeklyPlayerIntel(name="X", usage_trend=75.0)
        assert week.usage_score(entry) == pytest.approx(0.75)


class TestMomentumScore:
    """B5 -- week.momentum_score, the SCORING-trend counterpart to
    usage_score (opportunity trend). Same contract, different field."""

    def test_none_entry_is_zero(self):
        assert week.momentum_score(None) == 0.0

    def test_no_momentum_is_zero(self):
        entry = week.WeeklyPlayerIntel(name="X")
        assert week.momentum_score(entry) == 0.0

    def test_scales_to_unit_range(self):
        entry = week.WeeklyPlayerIntel(name="X", momentum=60.0)
        assert week.momentum_score(entry) == pytest.approx(0.60)


class TestDivergenceScore:
    def test_none_entry_is_zero(self):
        assert week.divergence_score(None) == 0.0

    def test_no_divergence_is_zero(self):
        entry = week.WeeklyPlayerIntel(name="X")
        assert week.divergence_score(entry) == 0.0

    def test_scales_to_unit_range(self):
        entry = week.WeeklyPlayerIntel(name="X", divergence=40.0)
        assert week.divergence_score(entry) == pytest.approx(0.40)


class TestSpiceBonusMomentumAndDivergence:
    def test_zero_weights_are_exact_noop(self):
        cfg = _spicy(momentum_weight=0.0, divergence_weight=0.0)
        p = _p("X", "WR")
        w = week.WeeklyIntel(players={"x": week.WeeklyPlayerIntel(name="X", momentum=99.0, divergence=99.0)})
        assert week.spice_bonus(p, w, cfg, scale=100.0) == 0.0

    def test_momentum_weight_adds_a_bonus(self):
        cfg = _spicy(momentum_weight=0.3, volatility_weight=0.0, upside_lean_weight=0.0, usage_weight=0.0)
        p = _p("Hot", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"hot": week.WeeklyPlayerIntel(name="Hot", momentum=80.0)})
        assert week.spice_bonus(p, w, cfg, scale=100.0) > 0.0

    def test_divergence_weight_adds_a_bonus(self):
        cfg = _spicy(divergence_weight=0.3, volatility_weight=0.0, upside_lean_weight=0.0, usage_weight=0.0)
        p = _p("Diverging", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"diverging": week.WeeklyPlayerIntel(name="Diverging", divergence=80.0)})
        assert week.spice_bonus(p, w, cfg, scale=100.0) > 0.0

    def test_momentum_and_divergence_are_not_lean_scaled(self):
        cfg = _spicy(
            momentum_weight=0.3, divergence_weight=0.3,
            volatility_weight=0.0, upside_lean_weight=0.0, usage_weight=0.0,
            matchup_variance_weight=0.9,
        )
        p = _p("X", "WR", proj=10.0)
        w = week.WeeklyIntel(players={"x": week.WeeklyPlayerIntel(name="X", momentum=80.0, divergence=80.0)})
        favored = week.spice_bonus(p, w, cfg, scale=100.0, lean=1.0)
        underdog = week.spice_bonus(p, w, cfg, scale=100.0, lean=-1.0)
        assert favored == pytest.approx(underdog)


class TestTeamProjectedTotal:
    def test_matches_direct_optimizer_call(self):
        players = [
            mk_bp("Qb1", "QB", points=300.0, team="BUF"),
            mk_bp("Rb1", "RB", points=200.0, team="BUF"),
        ]
        board = Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})
        roster_positions = {"QB": 1, "RB": 1, "BN": 2}
        cfg = Config(roster_positions=roster_positions)
        total = week.team_projected_total(["qb1:QB", "rb1:RB"], board, roster_positions, cfg)
        assert total == pytest.approx(500.0)

    def test_unmatched_keys_are_silently_skipped(self):
        players = [mk_bp("Qb1", "QB", points=300.0, team="BUF")]
        board = Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})
        roster_positions = {"QB": 1, "BN": 2}
        cfg = Config(roster_positions=roster_positions)
        total = week.team_projected_total(["qb1:QB", "does-not-exist:RB"], board, roster_positions, cfg)
        assert total == pytest.approx(300.0)


class TestBuildWeekBriefMatchupLean:
    def _roster_positions(self):
        return {"QB": 1, "WR": 1, "BN": 5}

    def _board(self):
        players = [
            mk_bp("My Qb", "QB", points=300.0, team="BUF"),
            mk_bp("Rival Qb", "QB", points=100.0, team="MIA"),
        ]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def test_omitted_board_and_rosters_stays_a_noop(self):
        cfg = Config(roster_positions=self._roster_positions(), season=SeasonConfig(matchup_variance_weight=0.9))
        cfg.league = LeagueScoring(playoff_teams=4, my_opponent="Rival")
        roster = [_p("My Qb", "QB", proj=20.0, team="BUF")]
        # No board/league_rosters passed -- must not raise, and must be
        # bit-identical to the same call with lean forced to 0.0.
        brief = week.build_week_brief(roster, self._roster_positions(), week=3, cfg=cfg)
        assert brief.lineup is not None

    def test_underdog_gets_a_bigger_lineup_boost_than_favorite(self):
        # Same intel-driven boom bonus on both sides; only the matchup
        # (my_opponent's roster strength) differs between the two calls.
        roster_positions = self._roster_positions()
        w = week.WeeklyIntel(players={"my qb": week.WeeklyPlayerIntel(name="My Qb", volatility=95.0)})

        cfg = Config(
            roster_positions=roster_positions,
            season=SeasonConfig(volatility_weight=0.4, matchup_variance_weight=0.8),
        )
        cfg.league = LeagueScoring(playoff_teams=4, my_opponent="Rival")
        roster = [_p("My Qb", "QB", proj=20.0, team="BUF")]

        weak_rival_board = Board(
            players=[mk_bp("My Qb", "QB", points=300.0, team="BUF"), mk_bp("Rival Qb", "QB", points=10.0, team="MIA")],
            by_key={}, replacement={}, starters_per_pos={}, tier_last={},
        )
        weak_rival_board.by_key = {p.key: p for p in weak_rival_board.players}
        strong_rival_board = Board(
            players=[mk_bp("My Qb", "QB", points=300.0, team="BUF"), mk_bp("Rival Qb", "QB", points=290.0, team="MIA")],
            by_key={}, replacement={}, starters_per_pos={}, tier_last={},
        )
        strong_rival_board.by_key = {p.key: p for p in strong_rival_board.players}

        rosters = LeagueRosters(teams={"Rival": ["Rival Qb"]})

        favored_brief = week.build_week_brief(
            roster, roster_positions, week=3, cfg=cfg, weekly=w,
            board=weak_rival_board, league_rosters=rosters,
        )
        underdog_brief = week.build_week_brief(
            roster, roster_positions, week=3, cfg=cfg, weekly=w,
            board=strong_rival_board, league_rosters=rosters,
        )
        favored_pts = favored_brief.lineup.assignments[0][1].projected_points
        underdog_pts = underdog_brief.lineup.assignments[0][1].projected_points
        assert underdog_pts > favored_pts
