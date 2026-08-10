from __future__ import annotations

import pytest

from ffbot import denial
from ffbot.board import Board
from ffbot.config import Config, DraftConfig, LeagueScoring, SeasonConfig, TeamStanding
from ffbot.league_rosters import LeagueRosters
from tests.conftest import mk_bp


class TestThreat:
    def test_eliminated_team_is_zero(self):
        s = TeamStanding(name="X", seed=1, eliminated=True)
        assert denial.threat(s, playoff_teams=4, num_teams=12) == 0.0

    def test_team_at_the_bubble_is_maximal(self):
        s = TeamStanding(name="X", seed=4)
        assert denial.threat(s, playoff_teams=4, num_teams=12) == 1.0

    def test_far_from_bubble_fades(self):
        near = denial.threat(TeamStanding(name="A", seed=5), playoff_teams=4, num_teams=12)
        far = denial.threat(TeamStanding(name="B", seed=12), playoff_teams=4, num_teams=12)
        assert 0.0 <= far < near <= 1.0

    def test_unknown_standing_is_moderate_not_zero_or_max(self):
        assert denial.threat(None, playoff_teams=4, num_teams=12) == 0.5
        assert denial.threat(TeamStanding(name="X"), playoff_teams=4, num_teams=12) == 0.5

    def test_new_signals_are_a_noop_at_defaults(self):
        # Same call as test_far_from_bubble_fades, but explicitly passing
        # every new kwarg at its zero/off default -- must be bit-identical.
        s = TeamStanding(name="Rival", seed=5)
        bare = denial.threat(s, playoff_teams=4, num_teams=12)
        explicit = denial.threat(
            s, playoff_teams=4, num_teams=12,
            team_name="Rival", my_opponent="", my_seed=5,
            opponent_boost=0.0, seed_window=0, playoff_push=True,
        )
        assert bare == explicit

    def test_opponent_boost_applies_only_to_the_named_opponent(self):
        s = TeamStanding(name="Rival", seed=10)  # far from bubble, low base threat
        boosted = denial.threat(
            s, playoff_teams=4, num_teams=12,
            team_name="Rival", my_opponent="Rival", opponent_boost=0.6,
        )
        not_my_opponent = denial.threat(
            s, playoff_teams=4, num_teams=12,
            team_name="Rival", my_opponent="Someone Else", opponent_boost=0.6,
        )
        assert boosted > not_my_opponent
        assert not_my_opponent == denial.threat(s, playoff_teams=4, num_teams=12)

    def test_opponent_boost_cannot_rescue_an_eliminated_team(self):
        s = TeamStanding(name="Rival", seed=1, eliminated=True)
        assert denial.threat(
            s, playoff_teams=4, num_teams=12,
            team_name="Rival", my_opponent="Rival", opponent_boost=1.0,
        ) == 0.0

    def test_seed_window_boosts_a_nearby_rival_only_during_playoff_push(self):
        s = TeamStanding(name="Rival", seed=10)  # far from bubble
        during_push = denial.threat(
            s, playoff_teams=4, num_teams=12,
            my_seed=9, seed_window=2, playoff_push=True,
        )
        before_push = denial.threat(
            s, playoff_teams=4, num_teams=12,
            my_seed=9, seed_window=2, playoff_push=False,
        )
        assert during_push == 1.0  # pushed the rest of the way to max
        assert before_push == denial.threat(s, playoff_teams=4, num_teams=12)  # unboosted base

    def test_seed_window_ignores_a_rival_outside_the_window(self):
        s = TeamStanding(name="Rival", seed=10)
        out_of_window = denial.threat(
            s, playoff_teams=4, num_teams=12,
            my_seed=1, seed_window=2, playoff_push=True,
        )
        assert out_of_window == denial.threat(s, playoff_teams=4, num_teams=12)

    def test_combined_boosts_are_capped_at_one(self):
        s = TeamStanding(name="Rival", seed=4)  # already at max base threat
        result = denial.threat(
            s, playoff_teams=4, num_teams=12,
            team_name="Rival", my_opponent="Rival", opponent_boost=0.9,
            my_seed=4, seed_window=2, playoff_push=True,
        )
        assert result == 1.0


class TestIsPlayoffPush:
    def test_missing_league_is_false(self):
        assert denial.is_playoff_push(None) is False

    def test_missing_week_is_false(self):
        league = LeagueScoring(regular_season_weeks=14)
        assert denial.is_playoff_push(league) is False

    def test_early_week_is_false(self):
        league = LeagueScoring(week=5, regular_season_weeks=14)
        assert denial.is_playoff_push(league) is False

    def test_final_weeks_are_true(self):
        league = LeagueScoring(week=12, regular_season_weeks=14)
        assert denial.is_playoff_push(league) is True

    def test_last_week_is_true(self):
        league = LeagueScoring(week=14, regular_season_weeks=14)
        assert denial.is_playoff_push(league) is True


class TestRivalRosterKeys:
    def test_matches_by_normalized_name(self):
        board = Board(
            players=[mk_bp("Josh Allen", "QB"), mk_bp("Bijan Robinson", "RB")],
            by_key={}, replacement={}, starters_per_pos={}, tier_last={},
        )
        board.by_key = {p.key: p for p in board.players}
        keys = denial.rival_roster_keys(["Josh Allen", "Ja'Marr Chase"], board)
        assert keys == ["josh allen:QB"]  # Chase has no board entry -- silently skipped, not raised


class TestDenialValue:
    def _board(self):
        players = [
            mk_bp("Contested Rb", "RB", points=150.0, vor=100.0),
            mk_bp("Rival Qb", "QB", points=300.0, vor=50.0),
        ]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def _cfg(self, denial_weight=1.0, teams=None, playoff_teams=4):
        league = LeagueScoring(playoff_teams=playoff_teams, teams=teams or [])
        cfg = Config(
            roster_positions={"RB": 1, "QB": 1, "BN": 1},
            draft=DraftConfig(num_teams=12),
            season=SeasonConfig(denial_weight=denial_weight),
        )
        cfg.league = league
        return cfg

    def test_zero_when_weight_is_zero(self):
        cfg = self._cfg(denial_weight=0.0, teams=[TeamStanding(name="Rival", seed=4)])
        rosters = LeagueRosters(teams={"Rival": ["Rival Qb"]})
        bp = self._board().by_key["contested rb:RB"]
        assert denial.denial_value(bp, rosters, self._board(), cfg.roster_positions, cfg) == 0.0

    def test_zero_when_no_league_rosters(self):
        cfg = self._cfg(denial_weight=1.0)
        rosters = LeagueRosters()  # no teams
        bp = self._board().by_key["contested rb:RB"]
        assert denial.denial_value(bp, rosters, self._board(), cfg.roster_positions, cfg) == 0.0

    def test_positive_when_rival_actually_needs_the_position(self):
        # Rival has no RB at all -- Contested Rb would be a real starter for them.
        cfg = self._cfg(denial_weight=1.0, teams=[TeamStanding(name="Rival", seed=4)])
        rosters = LeagueRosters(teams={"Rival": ["Rival Qb"]})
        board = self._board()
        bp = board.by_key["contested rb:RB"]
        dv = denial.denial_value(bp, rosters, board, cfg.roster_positions, cfg)
        assert dv > 0.0

    def test_zero_when_rival_is_eliminated(self):
        cfg = self._cfg(denial_weight=1.0, teams=[TeamStanding(name="Rival", seed=4, eliminated=True)])
        rosters = LeagueRosters(teams={"Rival": ["Rival Qb"]})
        board = self._board()
        bp = board.by_key["contested rb:RB"]
        assert denial.denial_value(bp, rosters, board, cfg.roster_positions, cfg) == 0.0

    def test_weight_scales_linearly(self):
        teams = [TeamStanding(name="Rival", seed=4)]
        rosters = LeagueRosters(teams={"Rival": ["Rival Qb"]})
        board = self._board()
        bp = board.by_key["contested rb:RB"]
        low = denial.denial_value(bp, rosters, board, self._cfg(denial_weight=0.5, teams=teams).roster_positions,
                                   self._cfg(denial_weight=0.5, teams=teams))
        high = denial.denial_value(bp, rosters, board, self._cfg(denial_weight=1.0, teams=teams).roster_positions,
                                    self._cfg(denial_weight=1.0, teams=teams))
        assert high == pytest.approx(low * 2, rel=1e-6)

    def test_opponent_boost_wired_through_from_league_yml(self):
        # Two rivals at the identical seed/gain -- only the one named
        # my_opponent should score higher, end to end through denial_value.
        teams = [TeamStanding(name="ThisWeekOpponent", seed=10), TeamStanding(name="SomeOtherRival", seed=10)]
        board = self._board()
        bp = board.by_key["contested rb:RB"]

        cfg = self._cfg(denial_weight=1.0, teams=teams)
        cfg.league.my_opponent = "ThisWeekOpponent"
        cfg.season.denial_opponent_boost = 0.5

        dv_opponent = denial.denial_value(
            bp, LeagueRosters(teams={"ThisWeekOpponent": []}), board, cfg.roster_positions, cfg,
        )
        dv_other = denial.denial_value(
            bp, LeagueRosters(teams={"SomeOtherRival": []}), board, cfg.roster_positions, cfg,
        )
        assert dv_opponent > dv_other

    def test_seed_window_wired_through_during_playoff_push(self):
        # A rival one seed away from me scores higher once the push window
        # and seed_window are both configured, end to end.
        teams = [TeamStanding(name="Me", seed=5), TeamStanding(name="NearbyRival", seed=6)]
        board = self._board()
        bp = board.by_key["contested rb:RB"]

        cfg = self._cfg(denial_weight=1.0, teams=teams)
        cfg.league.my_team = "Me"
        cfg.league.week = 14
        cfg.league.regular_season_weeks = 14
        cfg.season.denial_seed_window = 2

        dv_during_push = denial.denial_value(
            bp, LeagueRosters(teams={"NearbyRival": []}), board, cfg.roster_positions, cfg,
        )

        cfg_before_push = self._cfg(denial_weight=1.0, teams=teams)
        cfg_before_push.league.my_team = "Me"
        cfg_before_push.league.week = 3
        cfg_before_push.league.regular_season_weeks = 14
        cfg_before_push.season.denial_seed_window = 2
        dv_before_push = denial.denial_value(
            bp, LeagueRosters(teams={"NearbyRival": []}), board, cfg_before_push.roster_positions, cfg_before_push,
        )
        assert dv_during_push > dv_before_push

    def test_new_config_fields_default_to_off(self):
        cfg = self._cfg(denial_weight=1.0, teams=[TeamStanding(name="Rival", seed=4)])
        assert cfg.league.my_opponent == ""
        assert cfg.season.denial_opponent_boost == 0.0
        assert cfg.season.denial_seed_window == 0


class TestDenialCandidates:
    def _board(self):
        players = [
            mk_bp("Rostered Rb", "RB", points=180.0, vor=130.0),
            mk_bp("Contested Wr", "WR", points=150.0, vor=100.0),
            mk_bp("Boring Fa", "WR", points=30.0, vor=-20.0),
        ]
        return Board(players=players, by_key={p.key: p for p in players}, replacement={}, starters_per_pos={}, tier_last={})

    def test_empty_when_weight_is_zero(self):
        cfg = Config(roster_positions={"RB": 1, "WR": 1, "BN": 1}, season=SeasonConfig(denial_weight=0.0))
        cfg.league = LeagueScoring(playoff_teams=4, teams=[TeamStanding(name="Rival", seed=4)])
        rosters = LeagueRosters(teams={"Rival": []})
        out = denial.denial_candidates(
            [], self._board(), cfg.roster_positions, cfg, rosters, rostered_names=set(),
            streaming_floor=0.0,
        )
        assert out == []

    def test_fires_only_above_streaming_floor(self):
        cfg = Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 1},
            draft=DraftConfig(num_teams=12),
            season=SeasonConfig(denial_weight=1.0),
        )
        cfg.league = LeagueScoring(playoff_teams=4, teams=[TeamStanding(name="Rival", seed=4)])
        # Rival has no WR at all, so Contested Wr is a real gain for them.
        rosters = LeagueRosters(teams={"Rival": []})
        board = self._board()

        low_floor = denial.denial_candidates(
            [], board, cfg.roster_positions, cfg, rosters, rostered_names={"rostered rb"},
            streaming_floor=0.0,
        )
        names_low = [c.add_name for c in low_floor]
        assert "Contested Wr" in names_low

        high_floor = denial.denial_candidates(
            [], board, cfg.roster_positions, cfg, rosters, rostered_names={"rostered rb"},
            streaming_floor=10_000.0,
        )
        assert high_floor == []

    def test_rostered_names_excluded(self):
        cfg = Config(
            roster_positions={"RB": 1, "WR": 1, "BN": 1},
            draft=DraftConfig(num_teams=12),
            season=SeasonConfig(denial_weight=1.0),
        )
        cfg.league = LeagueScoring(playoff_teams=4, teams=[TeamStanding(name="Rival", seed=4)])
        rosters = LeagueRosters(teams={"Rival": []})
        board = self._board()
        out = denial.denial_candidates(
            [], board, cfg.roster_positions, cfg, rosters,
            rostered_names={"rostered rb", "contested wr"}, streaming_floor=0.0,
        )
        assert "Contested Wr" not in [c.add_name for c in out]


class TestCanDenyClaim:
    def test_no_floor_always_allows(self):
        from ffbot import policy
        cfg = Config(season=SeasonConfig(denial_priority_floor=0))
        assert policy.can_deny_claim(1, cfg).allowed

    def test_floor_refuses_valuable_priority(self):
        from ffbot import policy
        cfg = Config(season=SeasonConfig(denial_priority_floor=3))
        v = policy.can_deny_claim(2, cfg)
        assert not v.allowed
        assert "too valuable" in v.reason

    def test_floor_allows_cheap_priority(self):
        from ffbot import policy
        cfg = Config(season=SeasonConfig(denial_priority_floor=3))
        assert policy.can_deny_claim(10, cfg).allowed
