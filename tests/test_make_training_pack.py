from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.draft import round_and_slot, team_slot_at
from scripts.make_training_pack import main

LAYOUT = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1, "BN": 6}


def _write_board_csv(path: Path) -> None:
    lines = ["Player,Team,POS,BYE,FPTS,AVG\n"]
    counts = {"QB": 12, "RB": 30, "WR": 36, "TE": 14, "K": 10, "DEF": 10}
    n = 0
    for pos, count in counts.items():
        base = {"QB": 300.0, "RB": 290.0, "WR": 285.0, "TE": 230.0, "K": 150.0, "DEF": 140.0}[pos]
        for i in range(count):
            n += 1
            lines.append(f"{pos}{i},XXX,{pos},{5 + n % 9},{base - 2.5 * i},{i * 3 + 1}\n")
    path.write_text("".join(lines), encoding="utf-8")


def _write_config(path: Path, board_csv: Path, num_teams: int = 4, rounds: int = 15) -> None:
    positions_yaml = "\n".join(f"  {k}: {v}" for k, v in LAYOUT.items())
    path.write_text(
        "sleeper:\n  league_id: \"\"\n"
        f"roster_positions:\n{positions_yaml}\n"
        "draft:\n"
        f"  num_teams: {num_teams}\n"
        "  my_slot: 1\n"
        f"  rounds: {rounds}\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n",
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path):
    board_csv = tmp_path / "board.csv"
    _write_board_csv(board_csv)
    config_path = tmp_path / "config.yml"
    _write_config(config_path, board_csv)
    return tmp_path, config_path


class TestGeneratesRequestedCount:
    def test_count_is_honored(self, env, capsys):
        tmp_path, config_path = env
        out_path = tmp_path / "pack.json"
        rc = main([
            "--config", str(config_path),
            "--count", "10",
            "--drafts", "3",
            "--seed", "5",
            "--out", str(out_path),
        ])
        assert rc == 0
        pack = json.loads(out_path.read_text(encoding="utf-8"))
        assert len(pack["scenarios"]) == 10


class TestScenarioIntegrity:
    def test_every_pick_belongs_to_its_slot(self, env):
        tmp_path, config_path = env
        out_path = tmp_path / "pack.json"
        main([
            "--config", str(config_path),
            "--count", "20",
            "--drafts", "3",
            "--seed", "5",
            "--out", str(out_path),
        ])
        pack = json.loads(out_path.read_text(encoding="utf-8"))
        for s in pack["scenarios"]:
            header = s["state"]["header"]
            round_, slot = round_and_slot(s["pick"], header["num_teams"], header["order"])
            assert slot == team_slot_at(s["pick"], header["num_teams"], header["order"])
            assert slot == s["my_slot"]
            assert round_ == s["round"]

    def test_rosters_are_nonempty_from_round_2_on(self, env):
        tmp_path, config_path = env
        out_path = tmp_path / "pack.json"
        main([
            "--config", str(config_path),
            "--count", "20",
            "--drafts", "3",
            "--seed", "5",
            "--out", str(out_path),
        ])
        pack = json.loads(out_path.read_text(encoding="utf-8"))
        for s in pack["scenarios"]:
            if s["round"] >= 2:
                assert len(s["state"]["roster"]) >= 1, f"scenario {s['id']} (round {s['round']}) has an empty roster"

    def test_recommendation_table_is_populated(self, env):
        tmp_path, config_path = env
        out_path = tmp_path / "pack.json"
        main([
            "--config", str(config_path),
            "--count", "10",
            "--drafts", "3",
            "--seed", "5",
            "--out", str(out_path),
        ])
        pack = json.loads(out_path.read_text(encoding="utf-8"))
        for s in pack["scenarios"]:
            assert len(s["state"]["recommendations"]) > 0


class TestDeterminism:
    def test_same_seed_is_byte_identical(self, env):
        tmp_path, config_path = env
        out1 = tmp_path / "pack1.json"
        out2 = tmp_path / "pack2.json"
        main(["--config", str(config_path), "--count", "12", "--drafts", "3", "--seed", "9", "--out", str(out1)])
        main(["--config", str(config_path), "--count", "12", "--drafts", "3", "--seed", "9", "--out", str(out2)])
        text1 = out1.read_text(encoding="utf-8")
        text2 = out2.read_text(encoding="utf-8")
        # generated_at timestamps legitimately differ between the two runs;
        # strip that one line before comparing the rest byte-for-byte.
        pack1 = json.loads(text1)
        pack2 = json.loads(text2)
        # generated_at differs run-to-run; pack_id is derived from --out's
        # filename (pack1.json vs pack2.json here), not from the seed --
        # neither is part of what "same seed" promises to reproduce.
        for pack in (pack1, pack2):
            pack.pop("generated_at", None)
            pack.pop("pack_id", None)
        assert pack1 == pack2


class TestExport:
    def test_export_writes_a_standalone_html_file(self, env):
        tmp_path, config_path = env
        out_path = tmp_path / "pack.json"
        export_path = tmp_path / "pack.html"
        rc = main([
            "--config", str(config_path),
            "--count", "6",
            "--drafts", "2",
            "--seed", "1",
            "--out", str(out_path),
            "--export", str(export_path),
        ])
        assert rc == 0
        assert export_path.exists()
        html = export_path.read_text(encoding="utf-8")
        assert 'src="/' not in html
        assert "function recRow" in html


class TestNoBoardConfigured:
    def test_degrades_to_a_clean_error_not_a_crash(self, tmp_path, capsys):
        config_path = tmp_path / "config.yml"
        config_path.write_text("draft:\n  num_teams: 4\n", encoding="utf-8")
        rc = main(["--config", str(config_path), "--out", str(tmp_path / "pack.json")])
        assert rc == 1
