from __future__ import annotations

import pytest

from ffbot import week
from ffbot.board import Board
from ffbot.config import Config, SeasonConfig
from ffbot.models import Player
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


class TestWaiverCandidates:
    def _board(self, extra_bp=None):
        players = [
            mk_bp("Roster Rb", "RB", points=180.0, rank=1),
            mk_bp("Roster Wr", "WR", points=170.0, rank=2),
            mk_bp("Bench Scrub", "WR", points=40.0, rank=200),
            mk_bp("Waiver Gem", "WR", points=200.0, rank=3),
        ]
        if extra_bp:
            players.append(extra_bp)
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def _roster(self):
        # Weekly-scale numbers on purpose -- _season_scale_roster must
        # replace these with the board's season totals before comparing.
        return [
            _p("Roster Rb", "RB", proj=15.0),
            _p("Roster Wr", "WR", proj=14.0),
            _p("Bench Scrub", "WR", proj=3.0),
        ]

    def _layout(self):
        return {"RB": 1, "WR": 1, "BN": 3}

    def test_a_real_upgrade_is_ranked_and_paired_with_a_drop(self):
        cfg = Config(roster_positions=self._layout())
        candidates, missing = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, remaining_faab=100, cfg=cfg
        )
        assert missing == []
        names = [c.add_name for c in candidates]
        assert "Waiver Gem" in names
        top = candidates[0]
        assert top.drop_name is not None
        assert top.max_bid >= 0

    def test_rostered_players_are_excluded_from_candidates(self):
        cfg = Config(roster_positions=self._layout())
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, remaining_faab=100, cfg=cfg
        )
        assert "Roster Rb" not in [c.add_name for c in candidates]

    def test_unmatched_roster_player_is_reported_not_silently_dropped(self):
        cfg = Config(roster_positions=self._layout())
        roster = self._roster() + [_p("Waiver Pickup Not On Board", "WR", proj=5.0)]
        _, missing = week.waiver_candidates(
            roster, self._board(), cfg.roster_positions, remaining_faab=100, cfg=cfg
        )
        assert "Waiver Pickup Not On Board" in missing

    def test_uses_season_scale_not_weekly_scale(self):
        # The regression case: Roster Wr's weekly proj (14.0) must NOT be what
        # gets compared against the board's season-scale numbers (170.0+), or
        # every waiver candidate looks like a massive upgrade over a real
        # starter purely from the unit mismatch.
        cfg = Config(roster_positions=self._layout())
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, remaining_faab=100, cfg=cfg
        )
        # Waiver Gem (200) barely beats Roster Wr (170) in season terms --
        # the gain should be double digits, not the ~186-point gap a
        # weekly-vs-season mismatch (200 - 14) would produce.
        gem = next(c for c in candidates if c.add_name == "Waiver Gem")
        assert gem.value < 50.0

    def test_zero_remaining_faab_still_returns_candidates_with_zero_bid(self):
        cfg = Config(roster_positions=self._layout())
        candidates, _ = week.waiver_candidates(
            self._roster(), self._board(), cfg.roster_positions, remaining_faab=0, cfg=cfg
        )
        assert candidates and all(c.max_bid == 0 for c in candidates)


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
