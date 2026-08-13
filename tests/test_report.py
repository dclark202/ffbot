from __future__ import annotations

from pathlib import Path

import pytest

from ffbot import projections
from ffbot import report
from ffbot import sleeper_roster
from ffbot.projections.cache import ProjectionFetchError
from ffbot.scoring import StatLine
from ffbot.sleeper.cache import SleeperFetchError


def _write_board_csv(tmp_path: Path) -> Path:
    rows = [
        "Josh Allen,BUF,QB,7,320.0",
        "Bijan Robinson,ATL,RB,5,280.0",
        "Waiver Wr,MIA,WR,10,150.0",
    ]
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_config(tmp_path: Path, board_csv: Path, source: str = "board") -> Path:
    path = tmp_path / "config.yml"
    # intel_file points away from the real repo's draft/intel.yml (CWD-
    # relative, and would otherwise fuzzy-match hundreds of unrelated real
    # players against this tiny fixture board) -- a nonexistent path is the
    # documented missing-file no-op.
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  RB: 1\n  BN: 3\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"projection_source:\n  source: {source}\n",
        encoding="utf-8",
    )
    return path


def _write_config_with_roster_source(
    tmp_path: Path, board_csv: Path, roster_source: str = "file",
    sleeper_league_id: str = "", sleeper_roster_id=None, sleeper_username: str = "",
) -> Path:
    path = tmp_path / "config.yml"
    roster_id_line = f"  roster_id: {sleeper_roster_id}\n" if sleeper_roster_id is not None else ""
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  RB: 1\n  BN: 3\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"roster_source:\n  source: {roster_source}\n"
        f"sleeper:\n  league_id: \"{sleeper_league_id}\"\n  username: \"{sleeper_username}\"\n{roster_id_line}",
        encoding="utf-8",
    )
    return path


def _write_roster(tmp_path: Path, names: list[str]) -> Path:
    path = tmp_path / "roster.yml"
    path.write_text("players:\n" + "\n".join(f"  - {n}" for n in names) + "\n", encoding="utf-8")
    return path


def _fake_provider_ok(season: int, week: int) -> list[dict]:
    return [
        {"name": "Josh Allen", "team": "BUF", "position": "QB", "points": 24.5, "bye": None, "stats": StatLine()},
        {"name": "Waiver Wr", "team": "MIA", "position": "WR", "points": 18.0, "bye": None, "stats": StatLine()},
    ]


def _fake_provider_failing(season: int, week: int) -> list[dict]:
    raise ProjectionFetchError("network unreachable")


class TestLoadEverythingDefaults:
    def test_board_source_is_bit_identical_to_before(self, tmp_path):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="board")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.projection_source == "board"
        assert loaded.projection_alerts == []
        assert loaded.weekly_points == {}
        # The season-board fallback rate: 320.0 / 17 (default weeks_in_season)
        assert loaded.players[0].projected_points == pytest.approx(320.0 / 17)

    def test_csv_source_is_unaffected(self, tmp_path):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="csv")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.projection_source == "csv"
        assert loaded.projection_alerts == []
        assert loaded.weekly_points == {}


class TestLoadEverythingSleeper(object):
    def test_provider_rows_feed_the_roster(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_ok)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)

        assert loaded.projection_source == "sleeper"
        assert loaded.projection_alerts == []
        assert loaded.players[0].projected_points == 24.5  # real number, not 320/17

    def test_weekly_points_covers_the_whole_pool_not_just_the_roster(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_ok)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)

        # Waiver Wr is NOT on the roster but IS in the fake provider's rows --
        # confirms weekly_points is built from the whole fetch, not filtered
        # down to the roster the way the Player-matching path is.
        assert loaded.weekly_points["waiver wr:WR"] == 18.0
        assert loaded.weekly_points["josh allen:QB"] == 24.5

    def test_fetch_failure_falls_back_to_board_with_a_loud_alert(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_failing)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)

        assert loaded.projection_source == "sleeper"  # still the configured source...
        assert loaded.weekly_points == {}  # ...but no live numbers made it through
        assert len(loaded.projection_alerts) == 1
        assert "sleeper" in loaded.projection_alerts[0].lower()
        # Falls all the way back to the board-rescaled estimate, same as
        # the "board" source -- never raises, never silently zero-projects.
        assert loaded.players[0].projected_points == pytest.approx(320.0 / 17)

    def test_source_override_wins_over_config(self, tmp_path, monkeypatch):
        # Regression: an earlier version passed the RAW cfg.projection_source
        # (still "board") to resolve_provider even when source_override said
        # "sleeper" -- resolve_provider correctly returned None for "board",
        # and load_everything's own `assert provider is not None` blew up.
        # Caught only by asserting on what resolve_provider was actually
        # CALLED WITH, not just by stubbing its return value -- a lambda
        # that ignores its argument can't catch a wrong-argument bug.
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="board")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        seen_sources = []

        def fake_resolve_provider(source_cfg, **kw):
            seen_sources.append(source_cfg.source)
            return _fake_provider_ok

        monkeypatch.setattr(projections, "resolve_provider", fake_resolve_provider)

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1,
            season=2026, source_override="sleeper",
        )

        assert seen_sources == ["sleeper"]  # NOT "board", despite the config file
        assert loaded.projection_source == "sleeper"
        assert loaded.players[0].projected_points == 24.5


def _fake_ros_rows_ok(season, from_week, through_week, provider, league):
    return [{"name": "Josh Allen", "team": "BUF", "position": "QB", "points": 999.0, "bye": None}]


def _fake_ros_rows_failing(season, from_week, through_week, provider, league):
    raise ProjectionFetchError("ros network unreachable")


class TestLoadEverythingRosBoard:
    """The rest-of-season overlay board (ffbot.board.rescale_board_points +
    ffbot.projections.ros_rows), which is what makes week.waiver_candidates'
    ros_gain/hold_margin/drop_cost genuinely live -- see CLAUDE.md."""

    def test_sleeper_source_builds_a_real_ros_board(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_ok)
        monkeypatch.setattr(projections, "ros_rows", _fake_ros_rows_ok)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)

        assert loaded.ros_board is not None
        assert loaded.ros_board.by_key["josh allen:QB"].points == 999.0
        # The frozen season board is UNTOUCHED -- ros_board is additive,
        # never a replacement (season_board_rows' fixed-divisor contract
        # depends on `board` staying a true full-season total).
        assert loaded.board.by_key["josh allen:QB"].points == 320.0

    def test_board_source_never_calls_ros_rows(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="board")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding_ros_rows(*a, **kw):
            raise AssertionError("ros_rows must not be called for source=board")

        monkeypatch.setattr(projections, "ros_rows", exploding_ros_rows)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert loaded.ros_board is None

    def test_ros_fetch_failure_falls_back_to_none_with_a_loud_alert(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_ok)
        monkeypatch.setattr(projections, "ros_rows", _fake_ros_rows_failing)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)

        assert loaded.ros_board is None
        assert any("rest-of-season" in a.lower() for a in loaded.projection_alerts)
        # The weekly half is unaffected by the ROS-specific failure.
        assert loaded.players[0].projected_points == 24.5

    def test_weekly_fetch_failure_skips_ros_entirely(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding_ros_rows(*a, **kw):
            raise AssertionError("ros_rows must not run when the weekly fetch already failed")

        monkeypatch.setattr(projections, "resolve_provider", lambda cfg, **kw: _fake_provider_failing)
        monkeypatch.setattr(projections, "ros_rows", exploding_ros_rows)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1, season=2026)
        assert loaded.ros_board is None


class _FakeSleeperClientForRoster:
    """Stands in for ffbot.sleeper.client.SleeperClient — enough of it for
    the report.py roster_source="sleeper" path (players/ownership/rosters/
    user), exercising the REAL sleeper_roster functions against it rather
    than mocking those too."""

    PLAYERS = {"1": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "injury_status": "Questionable"}}
    OWNERSHIP = {"1": {"owned": 99.0, "started": 95.0}}

    def __init__(self, cache_dir=None):
        pass

    def players(self):
        return dict(self.PLAYERS)

    def ownership(self, season, week):
        return dict(self.OWNERSHIP)

    def rosters(self, league_id, **kwargs):
        return [{"roster_id": 4, "owner_id": "u1", "players": ["1"]}]

    def user(self, username):
        return {"user_id": "u1"} if username == "duncan" else None


class _FakeSleeperClientRaisingOnRosters(_FakeSleeperClientForRoster):
    def rosters(self, league_id, **kwargs):
        raise SleeperFetchError("simulated network failure")


class TestLoadEverythingRosterSourceSleeper:
    def test_file_source_is_bit_identical_to_before(self, tmp_path):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(tmp_path, board_csv, roster_source="file")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.roster_source == "file"
        assert loaded.roster_source_alerts == []
        assert loaded.players[0].status == ""  # never touched by the sleeper path

    def test_sleeper_source_with_explicit_roster_id_sets_live_identity(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForRoster)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.roster_source == "sleeper"
        assert loaded.roster_source_alerts == []
        [player] = loaded.players
        assert player.name == "Josh Allen"
        assert player.status == "Q"  # normalized from Sleeper's "Questionable"
        assert player.percent_owned == 99.0

    def test_sleeper_source_resolves_roster_id_from_username_when_unset(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_username="duncan",
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForRoster)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.roster_source_alerts == []
        assert loaded.players[0].name == "Josh Allen"

    def test_roster_yml_flags_still_merge_on_top_of_live_identity(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        roster = tmp_path / "roster.yml"
        roster.write_text("players:\n  - name: Josh Allen\n    undroppable: true\n", encoding="utf-8")
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForRoster)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.players[0].is_undroppable is True

    def test_fetch_failure_falls_back_to_roster_yml_with_a_loud_alert(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientRaisingOnRosters)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.roster_source == "sleeper"  # still the configured source...
        assert len(loaded.roster_source_alerts) == 1
        assert "sleeper" in loaded.roster_source_alerts[0].lower()
        # ...but falls all the way back to roster.yml identity, never raises.
        assert loaded.players[0].name == "Josh Allen"
        assert loaded.players[0].status == ""  # no live status applied on the fallback path

    def test_missing_roster_id_and_username_raises_a_clear_alert(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1")
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForRoster)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert len(loaded.roster_source_alerts) == 1
        assert "roster_id" in loaded.roster_source_alerts[0] or "username" in loaded.roster_source_alerts[0]
        assert loaded.players[0].name == "Josh Allen"  # still falls back to roster.yml

    def test_ownership_fetch_failure_degrades_quietly_identity_still_works(self, tmp_path, monkeypatch):
        class _NoOwnershipClient(_FakeSleeperClientForRoster):
            def ownership(self, season, week):
                raise SleeperFetchError("ownership endpoint down")

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _NoOwnershipClient)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.roster_source_alerts == []  # ownership is a nice-to-have -- no alert for its own failure
        assert loaded.players[0].name == "Josh Allen"
        assert loaded.players[0].percent_owned is None  # but the field it would have filled stays inert
