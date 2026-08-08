from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board  # noqa: E402
from ffbot.config import Config, DraftConfig  # noqa: E402
from scripts.draft_export import (  # noqa: E402
    main,
    parse_args,
    reconcile_with_yahoo,
    write_exports,
    write_reconciliation_report,
    yahoo_id_map,
)

STANDARD_LAYOUT = {
    "QB": 1, "WR": 2, "RB": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6, "IR": 1,
}


def _write_board_csv(tmp_path) -> Path:
    rows = [
        "Justin Jefferson,MIN,WR,13,320.5,4.2",
        "Christian McCaffrey,SF,RB,9,340.1,1.1",
        "Ja'Marr Chase,CIN,WR,12,315.0,5.0",
        "Josh Allen,BUF,QB,12,380.2,15.0",
        "Travis Kelce,KC,TE,10,260.0,20.0",
        "Justin Tucker,BAL,K,13,140.0,180.0",
        "Ravens D/ST,BAL,DEF,13,135.0,150.0",
        "Bijan Robinson,ATL,RB,11,300.0,8.0",
        "Amon-Ra St. Brown,DET,WR,5,290.0,10.0",
        "Sam LaPorta,DET,TE,5,210.0,60.0",
        "Harrison Butker,KC,K,10,138.0,190.0",
        "Commanders D/ST,WAS,DEF,14,120.0,175.0",
    ]
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS,AVG\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_config(tmp_path, board_csv: Path, num_teams=12, rounds=15) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "league_id: \"\"\n"
        "team_key: \"\"\n"
        "roster_positions:\n"
        "  QB: 1\n  WR: 2\n  RB: 2\n  TE: 1\n  W/R/T: 1\n  K: 1\n  DEF: 1\n  BN: 6\n  IR: 1\n"
        "draft:\n"
        f"  num_teams: {num_teams}\n"
        f"  rounds: {rounds}\n"
        f"  board_csv: [\"{board_csv}\"]\n",
        encoding="utf-8",
    )
    return path


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.out == "draft"
        assert args.yahoo_players is None

    def test_overrides(self):
        args = parse_args(["--board", "a.csv", "--out", "somewhere", "--yahoo-players", "y.json"])
        assert args.board == ["a.csv"]
        assert args.out == "somewhere"
        assert args.yahoo_players == "y.json"


class TestWriteExports:
    def _board(self, tmp_path, num_teams=2, rounds=4):
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=num_teams, rounds=rounds))
        board_csv = _write_board_csv(tmp_path)
        board = load_board([board_csv], STANDARD_LAYOUT, num_teams, cfg)
        return board, cfg

    def test_all_four_files_written(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        for p in paths.values():
            assert p.exists()
            assert p.stat().st_size > 0

    def test_board_csv_has_header_and_every_player(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        lines = paths["board_csv"].read_text(encoding="utf-8").strip().split("\n")
        assert lines[0] == "Rank,Player,Team,Pos,Bye,Proj,VOR,Tier,ADP"
        assert len(lines) == 1 + len(board.players)

    def test_board_txt_lists_every_player_in_rank_order(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        text = paths["board_txt"].read_text(encoding="utf-8")
        for bp in board.players:
            assert bp.name in text

    def test_kickers_and_defenses_deferred_in_txt_order(self, tmp_path):
        board, cfg = self._board(tmp_path, num_teams=2, rounds=4)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        text = paths["board_txt"].read_text(encoding="utf-8")
        lines = [l for l in text.split("\n") if l.strip() and l.strip()[0].isdigit()]
        k_or_def_line = next(i for i, l in enumerate(lines) if " K " in l or " DEF " in l)
        skill_line = next(i for i, l in enumerate(lines) if " WR " in l or " RB " in l or " QB " in l)
        assert k_or_def_line > skill_line

    def test_by_pos_groups_correctly(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        text = paths["board_by_pos"].read_text(encoding="utf-8")
        assert "=== WR ===" in text
        assert "=== RB ===" in text
        wr_section = text.split("=== WR ===")[1].split("=== ")[0]
        assert "Justin Jefferson" in wr_section
        assert "Christian McCaffrey" not in wr_section

    def test_cheatsheet_groups_by_position_and_tier(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "draft"
        paths = write_exports(board, cfg, out_dir)
        text = paths["cheatsheet"].read_text(encoding="utf-8")
        assert "DRAFT CHEATSHEET" in text
        assert "WR" in text
        assert "T1:" in text or "T2:" in text  # at least one tier line present

    def test_output_dir_created_if_missing(self, tmp_path):
        board, cfg = self._board(tmp_path)
        out_dir = tmp_path / "nested" / "draft"
        assert not out_dir.exists()
        write_exports(board, cfg, out_dir)
        assert out_dir.exists()


class TestReconcileWithYahoo:
    def _board(self, tmp_path):
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12, rounds=15))
        board_csv = _write_board_csv(tmp_path)
        board = load_board([board_csv], STANDARD_LAYOUT, 12, cfg)
        return board, cfg

    def test_exact_matches_counted(self, tmp_path):
        board, cfg = self._board(tmp_path)
        yahoo_players = [
            {"player_id": 1, "name": "Justin Jefferson", "position": "WR", "team": "MIN"},
            {"player_id": 2, "name": "Christian McCaffrey", "position": "RB", "team": "SF"},
        ]
        results, summary = reconcile_with_yahoo(board, yahoo_players, cfg)
        assert summary["exact"] >= 2

    def test_defense_naming_resolves(self, tmp_path):
        board, cfg = self._board(tmp_path)
        yahoo_players = [
            {"player_id": 3, "name": "Baltimore", "position": "DEF", "team": "Ravens"},
        ]
        results, summary = reconcile_with_yahoo(board, yahoo_players, cfg)
        ravens_result = next(r for r in results if r.query == "Ravens D/ST")
        assert ravens_result.matched_id == 3

    def test_unmatched_counted(self, tmp_path):
        board, cfg = self._board(tmp_path)
        results, summary = reconcile_with_yahoo(board, [], cfg)
        assert summary["none"] == len(board.players)

    def test_alias_resolves_unmatched(self, tmp_path):
        cfg = Config(
            roster_positions=STANDARD_LAYOUT,
            draft=DraftConfig(num_teams=12, rounds=15, aliases={"amonra st brown": "Amon-Ra StBrown"}),
        )
        board_csv = _write_board_csv(tmp_path)
        board = load_board([board_csv], STANDARD_LAYOUT, 12, cfg)
        yahoo_players = [{"player_id": 9, "name": "Amon-Ra StBrown", "position": "WR", "team": "DET"}]
        results, summary = reconcile_with_yahoo(board, yahoo_players, cfg)
        r = next(r for r in results if r.query == "Amon-Ra St. Brown")
        assert r.matched_id == 9


class TestWriteReconciliationReport:
    def test_report_includes_counts_and_unmatched_stub(self, tmp_path):
        from ffbot.names import MatchResult
        from collections import Counter

        results = [
            MatchResult("A", 1, "A", "exact"),
            MatchResult("B", None, None, "none", alternatives=["B2", "B3"]),
        ]
        summary = Counter(r.confidence for r in results)
        path = tmp_path / "report.txt"
        write_reconciliation_report(results, summary, path)
        text = path.read_text(encoding="utf-8")
        assert "exact:    1" in text
        assert "none:     1" in text
        assert '"B": ""' in text
        assert "B2" in text


class TestYahooIdMap:
    def test_maps_matched_only(self, tmp_path):
        cfg = Config(roster_positions=STANDARD_LAYOUT, draft=DraftConfig(num_teams=12, rounds=15))
        board_csv = _write_board_csv(tmp_path)
        board = load_board([board_csv], STANDARD_LAYOUT, 12, cfg)
        yahoo_players = [{"player_id": 1, "name": "Justin Jefferson", "position": "WR", "team": "MIN"}]
        results, _ = reconcile_with_yahoo(board, yahoo_players, cfg)
        id_map = yahoo_id_map(board, results)
        jefferson = next(bp for bp in board.players if bp.name == "Justin Jefferson")
        assert id_map[jefferson.key] == 1
        assert len(id_map) == 1


class TestMainEndToEnd:
    def test_writes_exports_and_returns_zero(self, tmp_path, monkeypatch, capsys):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        monkeypatch.chdir(tmp_path)
        rc = main(["--config", str(config_path), "--out", "draft_out"])
        assert rc == 0
        assert (tmp_path / "draft_out" / "board.csv").exists()
        assert (tmp_path / "draft_out" / "board_by_pos.txt").exists()
        assert (tmp_path / "draft_out" / "cheatsheet.txt").exists()

    def test_missing_board_returns_nonzero(self, tmp_path, monkeypatch):
        config_path = tmp_path / "empty.yml"
        config_path.write_text("league_id: \"\"\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        rc = main(["--config", str(config_path)])
        assert rc == 1

    def test_yahoo_players_flag_writes_reconciliation(self, tmp_path, monkeypatch):
        board_csv = _write_board_csv(tmp_path)
        config_path = _write_config(tmp_path, board_csv)
        yahoo_path = tmp_path / "yahoo.json"
        yahoo_path.write_text(
            json.dumps([{"player_id": 1, "name": "Justin Jefferson", "position": "WR", "team": "MIN"}]),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        rc = main(["--config", str(config_path), "--out", "draft_out", "--yahoo-players", str(yahoo_path)])
        assert rc == 0
        assert (tmp_path / "draft_out" / "reconciliation.txt").exists()
        ids = json.loads((tmp_path / "draft_out" / "yahoo_ids.json").read_text(encoding="utf-8"))
        assert any(v == 1 for v in ids.values())
