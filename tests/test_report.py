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


class TestLoadEverythingMissingBoardCsv:
    """The fresh-clone state: draft.board_csv is configured but the files
    don't exist yet (a manual FantasyPros download nobody's done). The
    weekly path must keep working end to end -- board=None, a surfaced
    board_alerts entry, never a raised FileNotFoundError -- as long as
    there's still SOME source of this-week points (here, a --proj CSV)."""

    def test_missing_board_degrades_with_surfaced_alert_not_a_crash(self, tmp_path):
        missing_board = tmp_path / "does_not_exist.csv"
        config = _write_config(tmp_path, missing_board, source="csv")
        roster = _write_roster(tmp_path, ["Christian McCaffrey"])
        proj = tmp_path / "weekly_rankings.csv"
        proj.write_text(
            "RK,PLAYER NAME,TEAM,POS,OPP,PROJ. FPTS\n"
            "1,Christian McCaffrey,SF,RB,DAL,22.4\n",
            encoding="utf-8",
        )

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1,
            proj_csv_paths=[str(proj)],
        )

        assert loaded.board is None
        assert len(loaded.board_alerts) == 1
        assert "download" in loaded.board_alerts[0].lower() or "exist yet" in loaded.board_alerts[0]
        assert loaded.players[0].name == "Christian McCaffrey"
        assert loaded.players[0].projected_points == pytest.approx(22.4)

    def test_present_board_has_no_board_alert(self, tmp_path):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv, source="board")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.board is not None
        assert loaded.board_alerts == []


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

    def __init__(self, cache_dir=None, **kwargs):
        pass

    def players(self):
        return dict(self.PLAYERS)

    def ownership(self, season, week):
        return dict(self.OWNERSHIP)

    def rosters(self, league_id, **kwargs):
        return [{"roster_id": 4, "owner_id": "u1", "players": ["1"], "settings": {"waiver_position": 6}}]

    def league(self, league_id, **kwargs):
        # Empty roster_positions -- starters_slot_map then zips against zero
        # non-bench slots against zero starters (the fake roster row above
        # carries no "starters" key), so this stays a no-op for every
        # existing identity-only test here; slot-mapping specifics get their
        # own dedicated test class below.
        return {"league_id": league_id, "roster_positions": []}

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
        assert loaded.waiver_priority == 6  # from the roster's own settings.waiver_position

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
        assert loaded.waiver_priority is None  # same never-crash degradation as roster identity

    def test_file_source_leaves_waiver_priority_none(self, tmp_path):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(tmp_path, board_csv, roster_source="file")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.waiver_priority is None

    def test_missing_waiver_position_on_the_roster_leaves_it_none(self, tmp_path, monkeypatch):
        class _NoWaiverPositionClient(_FakeSleeperClientForRoster):
            def rosters(self, league_id, **kwargs):
                return [{"roster_id": 4, "owner_id": "u1", "players": ["1"]}]  # no settings block at all

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _NoWaiverPositionClient)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.roster_source_alerts == []  # not an error -- a league with no waivers yet
        assert loaded.waiver_priority is None

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


def _write_config_with_kalshi_weight(tmp_path: Path, board_csv: Path, kalshi_weight: float) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  RB: 1\n  BN: 3\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"season:\n  kalshi_weight: {kalshi_weight}\n",
        encoding="utf-8",
    )
    return path


class TestLoadEverythingKalshiWeeklySignal:
    def test_zero_weight_never_touches_the_network(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.0)
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding(*a, **k):
            raise AssertionError("must not fetch when kalshi_weight is 0.0")

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", exploding)
        log_dir = tmp_path / "kalshi_log"
        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, kalshi_log_dir=str(log_dir),
        )
        assert loaded.game_conditions_alerts == []
        assert loaded.weekly.players.get("josh allen") is None or loaded.weekly.players["josh allen"].kalshi is None
        assert not log_dir.exists()  # B7: no fetch means no forward-log write either

    def test_nonzero_weight_merges_the_signal_into_weekly_players(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", lambda *a, **k: {"BUF": object()})
        monkeypatch.setattr("ffbot.markets.kalshi_nfl.weekly_signal", lambda *a, **k: {"josh allen:QB": 0.9})

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, kalshi_log_dir=str(tmp_path / "kalshi_log"),
        )
        assert loaded.game_conditions_alerts == []
        assert loaded.weekly.players["josh allen"].kalshi == pytest.approx(90.0)

    def test_schedule_fetch_failure_degrades_with_alert_never_raises(self, tmp_path, monkeypatch):
        from ffbot.live.schedule import ScheduleError

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def raising(*a, **k):
            raise ScheduleError("simulated network failure")

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", raising)
        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, kalshi_log_dir=str(tmp_path / "kalshi_log"),
        )
        assert len(loaded.game_conditions_alerts) == 1
        assert "Kalshi" in loaded.game_conditions_alerts[0]

    def test_hand_typed_kalshi_value_wins_over_the_fetched_one(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        weekly_path = tmp_path / "week-01.yml"
        weekly_path.write_text('players:\n  "Josh Allen":\n    kalshi: 12\n', encoding="utf-8")

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", lambda *a, **k: {"BUF": object()})
        monkeypatch.setattr("ffbot.markets.kalshi_nfl.weekly_signal", lambda *a, **k: {"josh allen:QB": 0.9})

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, weekly_path=str(weekly_path),
            kalshi_log_dir=str(tmp_path / "kalshi_log"),
        )
        assert loaded.weekly.players["josh allen"].kalshi == 12.0


class TestKalshiForwardLogging:
    """B7 -- `load_everything`'s forward-logging of the Kalshi weekly
    signal, piggybacked on the existing fetch (see ffbot/markets/
    kalshi_log.py's docstring for why this exists at all)."""

    def test_successful_fetch_writes_a_log_entry(self, tmp_path, monkeypatch):
        import json

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        log_dir = tmp_path / "kalshi_log"

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", lambda *a, **k: {"BUF": object()})
        monkeypatch.setattr("ffbot.markets.kalshi_nfl.weekly_signal", lambda *a, **k: {"josh allen:QB": 0.9})

        report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, kalshi_log_dir=str(log_dir),
        )
        log_files = list(log_dir.glob("*.jsonl"))
        assert len(log_files) == 1
        rows = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["week"] == 1
        assert rows[0]["player_prop_scores"] == {"josh allen:QB": 0.9}

    def test_schedule_fetch_failure_writes_no_log_entry(self, tmp_path, monkeypatch):
        from ffbot.live.schedule import ScheduleError

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        log_dir = tmp_path / "kalshi_log"

        def raising(*a, **k):
            raise ScheduleError("simulated network failure")

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", raising)
        report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, kalshi_log_dir=str(log_dir),
        )
        assert not log_dir.exists()  # empty scores -> log_weekly_snapshot never creates the dir

    def test_default_log_dir_is_used_when_not_overridden(self, tmp_path, monkeypatch):
        # Confirms the parameter threading itself, not the default path's
        # exact location (which would pollute the real repo's data/ dir
        # during a test run) -- monkeypatch the module-level default instead.
        import ffbot.markets.kalshi_log as kalshi_log_mod

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_kalshi_weight(tmp_path, board_csv, kalshi_weight=0.3)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        fake_default = tmp_path / "fake_default_log_dir"
        monkeypatch.setattr(kalshi_log_mod, "DEFAULT_LOG_DIR", fake_default)

        monkeypatch.setattr("ffbot.live.schedule.this_week_games", lambda *a, **k: {"BUF": object()})
        monkeypatch.setattr("ffbot.markets.kalshi_nfl.weekly_signal", lambda *a, **k: {"josh allen:QB": 0.9})

        report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert list(fake_default.glob("*.jsonl"))


class TestMergeKalshiScores:
    def test_new_player_entry_created(self):
        from ffbot.board import Board, BoardPlayer
        from tests.conftest import mk_bp

        bp = mk_bp("Josh Allen", "QB")
        board = Board(players=[bp], by_key={bp.key: bp}, replacement={}, starters_per_pos={}, tier_last={})
        intel = report.week.WeeklyIntel()
        merged = report._merge_kalshi_scores(intel, board, {bp.key: 0.75})
        assert merged.players["josh allen"].kalshi == pytest.approx(75.0)

    def test_existing_entry_without_kalshi_gets_it_added(self):
        from ffbot.board import Board
        from tests.conftest import mk_bp

        bp = mk_bp("Josh Allen", "QB")
        board = Board(players=[bp], by_key={bp.key: bp}, replacement={}, starters_per_pos={}, tier_last={})
        intel = report.week.WeeklyIntel(
            players={"josh allen": report.week.WeeklyPlayerIntel(name="Josh Allen", status="Q")}
        )
        merged = report._merge_kalshi_scores(intel, board, {bp.key: 0.6})
        assert merged.players["josh allen"].kalshi == pytest.approx(60.0)
        assert merged.players["josh allen"].status == "Q"  # untouched

    def test_existing_hand_typed_kalshi_is_not_overwritten(self):
        from ffbot.board import Board
        from tests.conftest import mk_bp

        bp = mk_bp("Josh Allen", "QB")
        board = Board(players=[bp], by_key={bp.key: bp}, replacement={}, starters_per_pos={}, tier_last={})
        intel = report.week.WeeklyIntel(
            players={"josh allen": report.week.WeeklyPlayerIntel(name="Josh Allen", kalshi=5.0)}
        )
        merged = report._merge_kalshi_scores(intel, board, {bp.key: 0.99})
        assert merged.players["josh allen"].kalshi == 5.0

    def test_unmatched_board_key_ignored(self):
        from ffbot.board import Board

        board = Board(players=[], by_key={}, replacement={}, starters_per_pos={}, tier_last={})
        intel = report.week.WeeklyIntel()
        merged = report._merge_kalshi_scores(intel, board, {"nobody:qb": 0.9})
        assert merged.players == {}

    def test_empty_scores_returns_the_same_object(self):
        from ffbot.board import Board

        board = Board(players=[], by_key={}, replacement={}, starters_per_pos={}, tier_last={})
        intel = report.week.WeeklyIntel()
        assert report._merge_kalshi_scores(intel, board, {}) is intel


class _FakeSleeperClientForStandings:
    def __init__(self, cache_dir=None, **kwargs):
        pass

    def rosters(self, league_id, **kwargs):
        return [
            {"roster_id": 4, "owner_id": "u1", "settings": {"wins": 5, "losses": 3}},
            {"roster_id": 5, "owner_id": "u2", "settings": {"wins": 3, "losses": 5}},
        ]

    def league_users(self, league_id):
        return [
            {"user_id": "u1", "display_name": "Me", "metadata": {"team_name": "My Team"}},
            {"user_id": "u2", "display_name": "Rival", "metadata": {}},
        ]

    def matchups(self, league_id, week):
        return [{"roster_id": 4, "matchup_id": 1}, {"roster_id": 5, "matchup_id": 1}]

    def user(self, username):
        return {"user_id": "u1"} if username == "duncan" else None


class _FakeSleeperClientRaisingOnStandings(_FakeSleeperClientForStandings):
    def rosters(self, league_id):
        raise SleeperFetchError("simulated network failure")


def _write_league_yml(tmp_path: Path) -> Path:
    path = tmp_path / "league.yml"
    path.write_text("name: Test League\nregular_season_weeks: 14\n", encoding="utf-8")
    return path


def _write_config_with_standings_source(
    tmp_path: Path, board_csv: Path, league_path: Path, standings_source: str = "file",
    sleeper_league_id: str = "L1", sleeper_roster_id=4,
) -> Path:
    path = tmp_path / "config.yml"
    roster_id_line = f"  roster_id: {sleeper_roster_id}\n" if sleeper_roster_id is not None else ""
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  RB: 1\n  BN: 3\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"league_file: \"{league_path.as_posix()}\"\n"
        f"standings_source:\n  source: {standings_source}\n"
        f"sleeper:\n  league_id: \"{sleeper_league_id}\"\n{roster_id_line}",
        encoding="utf-8",
    )
    return path


class TestLoadEverythingStandingsSourceSleeper:
    def test_file_source_never_touches_the_network(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_standings_source(tmp_path, board_csv, league_path, standings_source="file")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding(*a, **k):
            raise AssertionError("must not fetch when standings_source is file")

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", exploding)
        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert loaded.standings_alerts == []
        assert loaded.cfg.league.teams == []

    def test_no_league_file_is_a_noop_even_with_sleeper_source(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)  # no league_file at all -- cfg.league stays None
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding(*a, **k):
            raise AssertionError("must not fetch when cfg.league is None")

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", exploding)
        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert loaded.standings_alerts == []
        assert loaded.cfg.league is None

    def test_sleeper_source_populates_teams_my_team_and_opponent(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_standings_source(tmp_path, board_csv, league_path, standings_source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForStandings)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=3)

        assert loaded.standings_alerts == []
        names = {t.name for t in loaded.cfg.league.teams}
        assert names == {"My Team", "Rival"}
        assert loaded.cfg.league.my_team == "My Team"
        assert loaded.cfg.league.my_opponent == "Rival"
        assert loaded.cfg.league.week == 3

    def test_hand_typed_league_yml_teams_win_over_live(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = tmp_path / "league.yml"
        league_path.write_text(
            "name: Test League\n"
            "my_team: \"Hand Typed Team\"\n"
            "teams:\n  - name: \"My Team\"\n    record: \"99-0\"\n    seed: 1\n",
            encoding="utf-8",
        )
        config = _write_config_with_standings_source(tmp_path, board_csv, league_path, standings_source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForStandings)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=3)

        by_name = {t.name: t for t in loaded.cfg.league.teams}
        assert by_name["My Team"].record == "99-0"  # hand-typed, untouched
        assert "Rival" in by_name  # live-only team still added
        assert loaded.cfg.league.my_team == "Hand Typed Team"

    def test_roster_id_resolved_from_username_when_unset(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_standings_source(
            tmp_path, board_csv, league_path, standings_source="sleeper", sleeper_roster_id=None,
        )
        config.write_text(config.read_text().replace('sleeper:\n  league_id: "L1"\n', 'sleeper:\n  league_id: "L1"\n  username: "duncan"\n'))
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForStandings)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=3)

        assert loaded.standings_alerts == []
        assert loaded.cfg.league.my_team == "My Team"

    def test_fetch_failure_degrades_with_alert_league_yml_untouched(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = tmp_path / "league.yml"
        league_path.write_text("name: Test League\nmy_team: \"Original\"\n", encoding="utf-8")
        config = _write_config_with_standings_source(tmp_path, board_csv, league_path, standings_source="sleeper")
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientRaisingOnStandings)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=3)

        assert len(loaded.standings_alerts) == 1
        assert "sleeper" in loaded.standings_alerts[0].lower()
        assert loaded.cfg.league.my_team == "Original"  # untouched by the failed fetch


class _FakeSleeperClientForOpponentStarters(_FakeSleeperClientForStandings):
    def players(self):
        return {
            "101": {"full_name": "Rival QB", "team": "SF", "position": "QB"},
            "102": {"full_name": "Rival WR", "team": "SF", "position": "WR"},
        }

    def matchups(self, league_id, week):
        return [
            {"roster_id": 4, "matchup_id": 1, "starters": ["201", "0"]},
            {"roster_id": 5, "matchup_id": 1, "starters": ["101", "102"]},
        ]


class _FakeSleeperClientRaisingOnOpponentMatchups(_FakeSleeperClientForOpponentStarters):
    def matchups(self, league_id, week):
        raise SleeperFetchError("simulated network failure")


class _FakeSleeperClientPlayersExplodes(_FakeSleeperClientForStandings):
    def players(self):
        raise AssertionError("must not fetch the players dump when opponent_correlation_weight is 0.0")


def _write_config_with_opponent_weight(tmp_path, board_csv, league_path, weight=0.2):
    path = tmp_path / "config.yml"
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  RB: 1\n  BN: 3\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"league_file: \"{league_path.as_posix()}\"\n"
        "standings_source:\n  source: sleeper\n"
        f"season:\n  spice_level: 1\n  opponent_correlation_weight: {weight}\n"
        "sleeper:\n  league_id: \"L1\"\n  roster_id: 4\n",
        encoding="utf-8",
    )
    return path


class TestLoadEverythingOpponentStarters:
    """Structural live-seam test per CLAUDE.md: zero weight never fetches,
    a live fetch populates opponent_starters, and a fetch failure degrades
    with a surfaced alert, never a crash."""

    def test_zero_weight_never_fetches_the_players_dump(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_opponent_weight(tmp_path, board_csv, league_path, weight=0.0)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientPlayersExplodes)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.opponent_starters == []
        assert loaded.opponent_alerts == []

    def test_success_populates_opponent_starters(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_opponent_weight(tmp_path, board_csv, league_path, weight=0.2)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForOpponentStarters)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.opponent_alerts == []
        names = {s.name for s in loaded.opponent_starters}
        assert names == {"Rival QB", "Rival WR"}
        assert all(s.team == "SF" for s in loaded.opponent_starters)
        assert {s.position for s in loaded.opponent_starters} == {"QB", "WR"}

    def test_fetch_failure_degrades_with_alert_never_raises(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        league_path = _write_league_yml(tmp_path)
        config = _write_config_with_opponent_weight(tmp_path, board_csv, league_path, weight=0.2)
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientRaisingOnOpponentMatchups)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.opponent_starters == []
        assert len(loaded.opponent_alerts) == 1
        assert "opponent" in loaded.opponent_alerts[0].lower()


def _write_config_with_league_rosters_source(
    tmp_path: Path, board_csv: Path, roster_source: str = "file", league_rosters_source: str = "file",
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
        f"league_rosters_source:\n  source: {league_rosters_source}\n"
        f"sleeper:\n  league_id: \"{sleeper_league_id}\"\n  username: \"{sleeper_username}\"\n{roster_id_line}",
        encoding="utf-8",
    )
    return path


class _FakeSleeperClientForLeagueRosters:
    def __init__(self, cache_dir=None, **kwargs):
        pass

    def rosters(self, league_id, **kwargs):
        return [
            {"roster_id": 4, "owner_id": "u1", "players": ["1"]},
            {"roster_id": 5, "owner_id": "u2", "players": ["2"]},
        ]

    def league_users(self, league_id):
        return [
            {"user_id": "u1", "display_name": "Me", "metadata": {"team_name": "My Team"}},
            {"user_id": "u2", "display_name": "Rival", "metadata": {"team_name": "Rival Team"}},
        ]

    def players(self):
        return {"1": {"full_name": "Josh Allen"}, "2": {"full_name": "Christian McCaffrey"}}

    def user(self, username):
        return None


class _FakeSleeperClientRaisingOnLeagueRosters(_FakeSleeperClientForLeagueRosters):
    def rosters(self, league_id, **kwargs):
        raise SleeperFetchError("simulated network failure")


class TestLoadEverythingLeagueRostersSourceSleeper:
    """Structural live-seam test per CLAUDE.md: file source never touches
    the network, a live fetch populates the exclusion set, and a fetch
    failure falls back to the YAML file with a surfaced alert, never a
    crash."""

    def test_file_source_never_touches_the_network(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_league_rosters_source(tmp_path, board_csv, league_rosters_source="file")
        roster = _write_roster(tmp_path, ["Josh Allen"])

        def exploding(*a, **k):
            raise AssertionError("must not fetch when league_rosters_source is file")

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", exploding)
        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert loaded.league_rosters_source == "file"
        assert loaded.league_rosters_alerts == []

    def test_sleeper_source_populates_teams(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_league_rosters_source(
            tmp_path, board_csv, league_rosters_source="sleeper", sleeper_league_id="L1",
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForLeagueRosters)

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1,
            league_rosters_path=str(tmp_path / "no_lr.yml"),
        )

        assert loaded.league_rosters_source == "sleeper"
        assert loaded.league_rosters_alerts == []
        assert loaded.league_rosters.teams == {
            "My Team": ["Josh Allen"],
            "Rival Team": ["Christian McCaffrey"],
        }
        assert loaded.league_rosters.week == 1

    def test_fetch_failure_falls_back_to_file_with_a_loud_alert(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        lr_path = tmp_path / "league_rosters.yml"
        lr_path.write_text("week: 1\nsource: paste\nteams:\n  Old Team:\n    - Old Player\n", encoding="utf-8")
        config = _write_config_with_league_rosters_source(
            tmp_path, board_csv, league_rosters_source="sleeper", sleeper_league_id="L1",
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientRaisingOnLeagueRosters)

        loaded = report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1, league_rosters_path=str(lr_path),
        )

        assert loaded.league_rosters_source == "file"
        assert len(loaded.league_rosters_alerts) == 1
        assert "sleeper" in loaded.league_rosters_alerts[0].lower()
        assert loaded.league_rosters.teams == {"Old Team": ["Old Player"]}

    def test_players_dump_fetched_once_when_roster_and_league_rosters_both_sleeper(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_league_rosters_source(
            tmp_path, board_csv, roster_source="sleeper", league_rosters_source="sleeper",
            sleeper_league_id="L1", sleeper_roster_id=4,
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])

        class _CountingClient(_FakeSleeperClientForRoster):
            players_call_count = 0

            def players(self):
                type(self).players_call_count += 1
                return dict(self.PLAYERS)

            def league_users(self, league_id):
                return [{"user_id": "u1", "display_name": "Me", "metadata": {"team_name": "My Team"}}]

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _CountingClient)

        report.load_everything(
            config_path=str(config), roster_path=str(roster), week_num=1,
            league_rosters_path=str(tmp_path / "no_lr.yml"),
        )

        assert _CountingClient.players_call_count == 1


class _FakeSleeperClientForSlots:
    PLAYERS = {
        "1": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "injury_status": None},
        "2": {"full_name": "Waiver Wr", "position": "WR", "team": "MIA", "injury_status": None},
    }

    def __init__(self, cache_dir=None, **kwargs):
        pass

    def players(self):
        return dict(self.PLAYERS)

    def ownership(self, season, week):
        return {}

    def rosters(self, league_id, **kwargs):
        return [{
            "roster_id": 4, "owner_id": "u1",
            "players": ["1", "2"],
            "starters": ["1", "0"],
            "reserve": [],
            "settings": {},
        }]

    def league(self, league_id, **kwargs):
        return {"roster_positions": ["QB", "WR", "BN", "BN"]}

    def user(self, username):
        return None


class TestLoadEverythingLiveSlots:
    def test_starters_set_selected_position(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientForSlots)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.slots_source == "sleeper"
        assert loaded.roster_source_alerts == []
        by_name = {p.name: p for p in loaded.players}
        assert by_name["Josh Allen"].selected_position == "QB"
        assert by_name["Waiver Wr"].selected_position == "BN"  # not a Sleeper starter -- stays bench

    def test_league_fetch_failure_degrades_slots_source_to_file(self, tmp_path, monkeypatch):
        class _NoLeagueClient(_FakeSleeperClientForSlots):
            def league(self, league_id, **kwargs):
                raise SleeperFetchError("simulated network failure")

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _NoLeagueClient)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert loaded.slots_source == "file"
        assert len(loaded.roster_source_alerts) == 1
        assert "slots" in loaded.roster_source_alerts[0].lower()
        # Identity fetch itself still succeeds despite the slots-only failure.
        assert {p.name for p in loaded.players} == {"Josh Allen", "Waiver Wr"}


class TestLoadEverythingRefresh:
    def test_refresh_true_forces_the_shared_client(self, tmp_path, monkeypatch):
        captured: dict = {}

        class _RecordingClient(_FakeSleeperClientForRoster):
            def __init__(self, cache_dir=None, **kwargs):
                captured["cache_dir"] = cache_dir
                captured.update(kwargs)

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _RecordingClient)

        report.load_everything(
            config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1, refresh=True,
        )

        assert captured.get("force_refresh") is True

    def test_refresh_false_is_default(self, tmp_path, monkeypatch):
        captured: dict = {}

        class _RecordingClient(_FakeSleeperClientForRoster):
            def __init__(self, cache_dir=None, **kwargs):
                captured.update(kwargs)

        board_csv = _write_board_csv(tmp_path)
        config = _write_config_with_roster_source(
            tmp_path, board_csv, roster_source="sleeper", sleeper_league_id="L1", sleeper_roster_id=4,
        )
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _RecordingClient)

        report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no_roster.yml"), week_num=1)

        assert captured.get("force_refresh") is False
