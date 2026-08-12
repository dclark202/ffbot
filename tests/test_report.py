from __future__ import annotations

from pathlib import Path

import pytest

from ffbot import projections
from ffbot import report
from ffbot.projections.cache import ProjectionFetchError
from ffbot.scoring import StatLine


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
