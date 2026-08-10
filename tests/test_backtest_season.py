from __future__ import annotations

import csv
import dataclasses
import io

import pytest

from ffbot.backtest.draft_sim import DraftResult
from ffbot.backtest.season import (
    _league_rosters_from_draft,
    _roster_rows_from_keys,
    simulate_season,
    static_opponent_points,
)
from ffbot.board import Board, BoardPlayer, _board_key, assign_tiers, derive_replacement
from ffbot.config import Config, LeagueScoring, PassingScoring, ReceivingScoring, RushingScoring
from ffbot.draft import DraftState

# --- A tiny 3-team synthetic league --------------------------------------
#
# Deliberately minimal: derive_replacement/hold_margin's math is sensitive
# to how much bench depth exists (dropping the only player in a dedicated
# slot always "costs" ~their whole season value, since the slot goes empty
# with nobody else on the board to justify a smaller cost) -- see the two
# scenarios below, which exploit exactly that to test both directions.


def _ppr() -> LeagueScoring:
    return LeagueScoring(
        passing=PassingScoring(yards_per_point=25, td=4, int=-2),
        rushing=RushingScoring(yards_per_point=10, td=6),
        receiving=ReceivingScoring(yards_per_point=10, td=6, reception=1.0),
    )


_ROWS = [
    {"name": "WR Drafted A", "position": "WR", "team": "MIA", "points": 100.0, "adp": 5.0, "adp_stdev": 1.0},
    {"name": "WR Drafted B", "position": "WR", "team": "NE", "points": 90.0, "adp": 15.0, "adp_stdev": 1.0},
    {"name": "WR Drafted C", "position": "WR", "team": "BUF", "points": 80.0, "adp": 25.0, "adp_stdev": 1.0},
    {"name": "RB Drafted A", "position": "RB", "team": "MIA", "points": 95.0, "adp": 8.0, "adp_stdev": 1.0},
    {"name": "RB Drafted B", "position": "RB", "team": "NE", "points": 85.0, "adp": 18.0, "adp_stdev": 1.0},
    {"name": "RB Drafted C", "position": "RB", "team": "BUF", "points": 75.0, "adp": 28.0, "adp_stdev": 1.0},
    {"name": "WR Free Agent", "position": "WR", "team": "KC", "points": 130.0, "adp": 60.0, "adp_stdev": 1.0},
    {"name": "RB Free Agent", "position": "RB", "team": "KC", "points": 120.0, "adp": 65.0, "adp_stdev": 1.0},
]

_PLAYER_FIELDS = [
    "player_display_name", "position", "team", "season", "week",
    "receptions", "receiving_yards", "receiving_tds", "carries", "rushing_yards", "rushing_tds",
    "fumbles_lost_total",
]
_TEAM_CSV = b"team,season,week,def_sacks,def_interceptions,def_fumbles,def_fumbles_forced,def_tds,special_teams_tds,def_safeties\n"
_GAMES_CSV = (
    "season,week,home_team,away_team,home_score,away_score,gameday,gametime\n"
    "2023,1,MIA,NE,20,10,2023-09-10,13:00\n2023,1,KC,BUF,24,17,2023-09-10,13:00\n"
    "2023,2,MIA,NE,20,10,2023-09-17,13:00\n2023,2,KC,BUF,24,17,2023-09-17,13:00\n"
).encode("utf-8")


def _prow(name, pos, team, week, rec=0, recyd=0, rectd=0, car=0, rushyd=0, rushtd=0):
    return {
        "player_display_name": name, "position": pos, "team": team, "season": 2023, "week": week,
        "receptions": rec, "receiving_yards": recyd, "receiving_tds": rectd,
        "carries": car, "rushing_yards": rushyd, "rushing_tds": rushtd, "fumbles_lost_total": 0,
    }


def _player_csv() -> bytes:
    rows = []
    for wk in (1, 2):
        rows += [
            _prow("WR Drafted A", "WR", "MIA", wk, rec=4, recyd=40),
            _prow("RB Drafted A", "RB", "MIA", wk, car=10, rushyd=30),
            _prow("WR Free Agent", "WR", "KC", wk, rec=10, recyd=150, rectd=2),
            _prow("RB Free Agent", "RB", "KC", wk, car=20, rushyd=140, rushtd=2),
        ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_PLAYER_FIELDS, restval="")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _opener():
    player_csv = _player_csv()

    def opener(url: str) -> bytes:
        if "stats_player_week" in url:
            return player_csv
        if "stats_team_week" in url:
            return _TEAM_CSV
        if "schedules/games.csv" in url:
            return _GAMES_CSV
        if "injuries" in url:
            return b"season,week,team,full_name,report_status\n"
        raise AssertionError(f"unexpected fetch: {url}")
    return opener


def _build(roster_positions: dict[str, int], cfg: Config) -> Board:
    starters_per_pos, replacement = derive_replacement(_ROWS, roster_positions, num_teams=3, cfg=cfg)
    tiers = assign_tiers(_ROWS, cfg)
    players = []
    for row in _ROWS:
        key = _board_key(row["name"], row["position"])
        repl = replacement.get(row["position"], row["points"])
        players.append(BoardPlayer(
            key=key, name=row["name"], position=row["position"], team=row["team"],
            bye_week=None, points=row["points"], adp=row["adp"], adp_stdev=row["adp_stdev"],
            adp_spread=None, yahoo_id=None, tier=tiers.get(key, 1), vor=row["points"] - repl,
            rank=0, points_fp=row["points"], points_source="consensus",
        ))
    players.sort(key=lambda bp: -bp.vor)
    players = [dataclasses.replace(bp, rank=i + 1) for i, bp in enumerate(players)]
    return Board(
        players=players, by_key={bp.key: bp for bp in players},
        replacement=replacement, starters_per_pos=starters_per_pos, tier_last={},
    )


def _keys():
    return {
        r["name"]: _board_key(r["name"], r["position"])
        for r in _ROWS
    }


def _draft(board: Board, roster_positions: dict[str, int], team1_names: list[str]) -> DraftResult:
    k = _keys()
    team1 = [k[n] for n in team1_names]
    team2 = [k["WR Drafted B"], k["RB Drafted B"]]
    team3 = [k["WR Drafted C"], k["RB Drafted C"]]
    return DraftResult(
        rosters=[team1, team2, team3],
        state=DraftState(board=board, num_teams=3, my_slot=1, rounds=2, roster_positions=roster_positions),
    )


def _cfg(roster_positions: dict[str, int]) -> Config:
    cfg = Config(roster_positions=roster_positions)
    cfg.league = _ppr()
    cfg.draft.num_teams = 3
    cfg.draft.rounds = 2
    return cfg


class TestRosterRowsFromKeys:
    def test_resolves_board_keys_to_identity_rows(self):
        cfg = _cfg({"WR": 1, "RB": 1})
        board = _build({"WR": 1, "RB": 1}, cfg)
        k = _keys()
        rows = _roster_rows_from_keys([k["WR Drafted A"], k["RB Drafted A"]], board)
        assert [r["name"] for r in rows] == ["WR Drafted A", "RB Drafted A"]
        assert rows[0]["team"] == "MIA"

    def test_unknown_key_is_skipped_not_an_error(self):
        cfg = _cfg({"WR": 1, "RB": 1})
        board = _build({"WR": 1, "RB": 1}, cfg)
        rows = _roster_rows_from_keys(["nonexistent:WR"], board)
        assert rows == []


class TestLeagueRostersFromDraft:
    def test_excludes_the_agent_slot_and_includes_the_rest(self):
        rp = {"WR": 1, "RB": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])
        lr = _league_rosters_from_draft(draft, board, agent_slot=1)
        all_names = lr.rostered_names()
        assert "wr drafted a" not in all_names  # agent's own roster excluded
        assert "wr drafted b" in all_names
        assert "rb drafted c" in all_names


class TestSimulateSeason:
    def test_open_bench_spot_claims_a_clearly_profitable_free_agent(self, tmp_path):
        # WR Free Agent projects far ahead of anyone on the board (naive
        # engine, from real per-game production) and the roster has an open
        # BN slot -- drop_cost is exactly 0 for an open spot (see
        # week.waiver_candidates' docstring, point 3), so this claim should
        # clear net > 0 easily.
        rp = {"WR": 1, "RB": 1, "BN": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])  # BN left empty on purpose

        result = simulate_season(
            2023, cfg, board, draft, agent_slot=1, policy="control", source="naive",
            weeks=[2], cache_dir=tmp_path, opener=_opener(),
        )
        assert result.adds == [(2, "WR Free Agent")]
        assert {r["name"] for r in result.final_roster} == {"WR Drafted A", "RB Drafted A", "WR Free Agent"}

    def test_full_roster_protects_a_real_asset_over_a_short_hot_streak(self, tmp_path):
        # No bench spot at all: dropping RB Drafted A (95 season points,
        # well above the RB replacement level) to chase a two-week hot
        # streak costs nearly RB Drafted A's entire season value -- net
        # should stay negative and NO claim should happen, even though the
        # free agent's recent per-game production is much better.
        rp = {"WR": 1, "RB": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])

        result = simulate_season(
            2023, cfg, board, draft, agent_slot=1, policy="control", source="naive",
            weeks=[2], cache_dir=tmp_path, opener=_opener(),
        )
        assert result.adds == []
        assert {r["name"] for r in result.final_roster} == {"WR Drafted A", "RB Drafted A"}

    def test_points_by_week_covers_every_requested_week(self, tmp_path):
        rp = {"WR": 1, "RB": 1, "BN": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])

        result = simulate_season(
            2023, cfg, board, draft, agent_slot=1, policy="agent", source="naive",
            weeks=[2], cache_dir=tmp_path, opener=_opener(),
        )
        assert set(result.points_by_week) == {2}

    def test_agent_and_control_are_identical_when_every_spice_weight_is_zero(self, tmp_path):
        # A bare Config() has every SeasonConfig weight at its dataclass
        # default of 0.0 -- adjusted_players is then an exact no-op, so the
        # two policies must produce bit-identical lineups and waiver moves.
        rp = {"WR": 1, "RB": 1, "BN": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])

        agent = simulate_season(
            2023, cfg, board, draft, agent_slot=1, policy="agent", source="naive",
            weeks=[2], cache_dir=tmp_path, opener=_opener(),
        )
        control = simulate_season(
            2023, cfg, board, draft, agent_slot=1, policy="control", source="naive",
            weeks=[2], cache_dir=tmp_path, opener=_opener(),
        )
        assert agent.points_by_week == control.points_by_week
        assert agent.adds == control.adds

    def test_rejects_an_unknown_policy(self, tmp_path):
        rp = {"WR": 1, "RB": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])
        with pytest.raises(ValueError):
            simulate_season(
                2023, cfg, board, draft, agent_slot=1, policy="bogus", source="naive",
                weeks=[2], cache_dir=tmp_path, opener=_opener(),
            )


class TestStaticOpponentPoints:
    def test_returns_points_for_every_requested_week(self, tmp_path):
        rp = {"WR": 1, "RB": 1}
        cfg = _cfg(rp)
        board = _build(rp, cfg)
        draft = _draft(board, rp, ["WR Drafted A", "RB Drafted A"])

        # Team 3's roster (WR/RB Drafted C) has no stat rows in the fixture
        # at all -- confirms this degrades to 0.0 per player rather than
        # crashing, the same "absent means unscored" contract every other
        # grading function in ffbot.backtest uses.
        points = static_opponent_points(
            2023, cfg, board, draft, team_slot=3, source="naive", weeks=[2],
            cache_dir=tmp_path, opener=_opener(),
        )
        assert points == {2: 0.0}
