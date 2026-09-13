"""Live-seam tests (per CLAUDE.md) for the two Sleeper reads added so the
report matches the Sleeper app: the league's scoring_settings, and this
week's real points. Each must work live, and degrade with a surfaced alert
rather than crash."""

from __future__ import annotations

from ffbot import report
from ffbot.sleeper.cache import SleeperFetchError
from tests.test_report import (
    _FakeSleeperClientForScoring,
    _FakeSleeperClientForSlots,
    _FakeSleeperClientRaisingOnLeagueForScoring,
    _write_board_csv,
    _write_config_with_standings_source,
    _write_roster,
)


def _league_with_saved_settings(tmp_path):
    path = tmp_path / "league.yml"
    path.write_text(
        "name: Test League\nsleeper_scoring_settings:\n  pass_td: 6.0\n", encoding="utf-8",
    )
    return path


class TestLiveScoringSettings:
    def test_live_settings_replace_the_saved_copy(self, tmp_path, monkeypatch):
        config = _write_config_with_standings_source(
            tmp_path, _write_board_csv(tmp_path), _league_with_saved_settings(tmp_path), standings_source="sleeper",
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])

        class _Client(_FakeSleeperClientForScoring):
            scoring_settings = {"pass_td": 4.0, "rec": 1.0}

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _Client)
        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)
        assert loaded.cfg.league.sleeper_scoring_settings == {"pass_td": 4.0, "rec": 1.0}

    def test_failed_fetch_keeps_the_saved_copy_with_an_alert(self, tmp_path, monkeypatch):
        config = _write_config_with_standings_source(
            tmp_path, _write_board_csv(tmp_path), _league_with_saved_settings(tmp_path), standings_source="sleeper",
        )
        roster = _write_roster(tmp_path, ["Josh Allen"])
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _FakeSleeperClientRaisingOnLeagueForScoring)

        loaded = report.load_everything(config_path=str(config), roster_path=str(roster), week_num=1)

        assert loaded.cfg.league.sleeper_scoring_settings == {"pass_td": 6.0}
        assert any("Live scoring settings" in a and "saved copy" in a for a in loaded.scoring_alerts)


def _write_live_config(tmp_path, board_csv, league_path):
    path = tmp_path / "config.yml"
    path.write_text(
        "roster_positions:\n  QB: 1\n  WR: 1\n  BN: 2\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 4\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
        f"league_file: \"{league_path.as_posix()}\"\n"
        "roster_source:\n  source: sleeper\n"
        "standings_source:\n  source: sleeper\n"
        "sleeper:\n  league_id: \"L1\"\n  roster_id: 4\n",
        encoding="utf-8",
    )
    return path


class _LiveClient(_FakeSleeperClientForSlots):
    def league_users(self, league_id):
        return [{"user_id": "u1", "display_name": "Me", "metadata": {}}]

    def matchups(self, league_id, week):
        return [{"roster_id": 4, "matchup_id": 1, "players_points": {"1": 21.4, "2": 0.0}}]


class TestLivePoints:
    def test_matchups_players_points_are_named(self, tmp_path, monkeypatch):
        league_path = tmp_path / "league.yml"
        league_path.write_text("name: Test League\n", encoding="utf-8")
        config = _write_live_config(tmp_path, _write_board_csv(tmp_path), league_path)
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _LiveClient)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no.yml"), week_num=1)

        assert loaded.live_points == {"josh allen": 21.4, "waiver wr": 0.0}
        assert loaded.live_points_alerts == []

    def test_failed_fetch_degrades_with_an_alert(self, tmp_path, monkeypatch):
        class _Broken(_LiveClient):
            def matchups(self, league_id, week):
                raise SleeperFetchError("simulated network failure")

        league_path = tmp_path / "league.yml"
        league_path.write_text("name: Test League\n", encoding="utf-8")
        config = _write_live_config(tmp_path, _write_board_csv(tmp_path), league_path)
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _Broken)

        loaded = report.load_everything(config_path=str(config), roster_path=str(tmp_path / "no.yml"), week_num=1)

        assert loaded.live_points == {}
        assert any("Live scores" in a for a in loaded.live_points_alerts)
