from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.week_report import main, parse_args, render_report, run_report


class TestRenderReport:
    def test_text_format_is_sections_joined_by_blank_line(self):
        out = render_report(["SECTION ONE\nline", "SECTION TWO\nline"], week_num=3, fmt="text")
        assert out == "SECTION ONE\nline\n\nSECTION TWO\nline"

    def test_text_format_adds_nothing_extra(self):
        out = render_report(["X"], week_num=3, fmt="text")
        assert out == "X"

    def test_markdown_format_wraps_in_header_and_fence(self):
        out = render_report(["SECTION ONE"], week_num=6, fmt="markdown")
        assert out.startswith("# Week 6 Report\n")
        assert "```\nSECTION ONE\n```" in out
        assert "Generated:" in out

    def test_markdown_preserves_exact_section_content_inside_fence(self):
        body = "WAIVERS\n--------\n  1) ADD Someone   net +5.0"
        out = render_report([body], week_num=1, fmt="markdown")
        assert body in out


def _write_board_csv(tmp_path: Path) -> Path:
    rows = [
        "Josh Allen,BUF,QB,7,320.0",
        "Bijan Robinson,ATL,RB,5,280.0",
    ]
    path = tmp_path / "board.csv"
    path.write_text("Player,Team,POS,BYE,FPTS\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_config(tmp_path: Path, board_csv: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(
        "roster_positions:\n  QB: 1\n  RB: 1\n  BN: 2\n"
        "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
        f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
        f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n",
        encoding="utf-8",
    )
    return path


def _write_roster(tmp_path: Path) -> Path:
    path = tmp_path / "roster.yml"
    path.write_text("players:\n  - Josh Allen\n  - Bijan Robinson\n", encoding="utf-8")
    return path


class TestMainOutAndQuiet:
    def test_out_writes_the_report_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        out_path = tmp_path / "reports" / "out.md"
        rc = main([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--out", str(out_path), "--format", "markdown", "--quiet",
        ])
        assert rc == 0
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert content.startswith("# Week 1 Report")
        assert "WEEK 1" in content

    def test_quiet_without_out_still_returns_zero_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        rc = main([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--quiet",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "WEEK 1" not in out  # report body suppressed

    def test_default_no_quiet_prints_to_stdout(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        rc = main([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "WEEK 1" in out

    def test_out_creates_parent_directories(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        out_path = tmp_path / "deep" / "nested" / "report.md"
        rc = main([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--out", str(out_path), "--quiet",
        ])
        assert rc == 0
        assert out_path.exists()

    def test_default_format_is_text_no_fence_no_header(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        out_path = tmp_path / "report.txt"
        rc = main([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--out", str(out_path), "--quiet",
        ])
        assert rc == 0
        content = out_path.read_text(encoding="utf-8")
        assert not content.startswith("# Week")
        assert content.startswith("WEEK 1")


class TestRunReportStructuredResults:
    def test_run_report_returns_structured_candidates_and_matches_rendered_output(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        args = parse_args([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--waivers", "--stream", "K",
        ])
        run = run_report(args)
        assert run.week == 1
        assert run.loaded is not None
        assert run.brief is not None
        assert isinstance(run.waivers, list)
        assert isinstance(run.waiver_missing, list)
        assert isinstance(run.ir_stash, list)
        assert isinstance(run.denial, list)
        assert "K" in run.streamers
        assert run.sections  # at least the WEEK N section

        # main() renders the exact same sections run_report already built.
        report_text = render_report(run.sections, args.week, args.format)
        assert "WEEK 1" in report_text

    def test_main_and_run_report_agree_on_rendered_output(self, tmp_path, monkeypatch, capsys):
        # --no-save-state on both calls -- otherwise the FIRST call's write
        # would make the SECOND call see a (correctly) different, already-
        # applied baseline, which isn't what this test is checking.
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        argv = [
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--no-save-state",
        ]
        main(argv)
        from_main = capsys.readouterr().out

        run = run_report(parse_args(argv))
        from_run_report = render_report(run.sections, 1, "text")
        assert from_main.strip() == from_run_report.strip()


class TestWeekBriefCallSitesAgree:
    """`scripts/week_report.py` and `ffbot/webapi.py` must build the week
    brief from the same inputs.

    Structural, not by inspection, and for a reason: they drifted. Commit
    `faeb6b5` added `opponent_starters=` to the GUI's call and not to this
    script's, so for every run of `week_report.py` -- and therefore every
    unattended `scripts/autorun.py` fire -- the LINEUP section was computed
    WITHOUT the opponent-stack adjustment while the RECOMMENDED START/SIT
    section printed directly below it (from `gameplan.build_gameplan`, which
    reads `loaded.opponent_starters` unconditionally) was computed WITH it.
    Two sections of one report could disagree, and the CLI could disagree
    with the GUI about the same week, with nothing on screen to say why.

    Comparing the kwarg sets is what catches the NEXT one: a new input
    wired into one entry point and forgotten at the other fails here rather
    than silently producing two different answers.
    """

    def _brief_call_kwargs(self, path: str) -> set[str]:
        import ast
        from pathlib import Path

        tree = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
        found: list[set[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "build_week_brief":
                found.append({kw.arg for kw in node.keywords if kw.arg})
        assert len(found) == 1, f"{path}: expected exactly one build_week_brief call, found {len(found)}"
        return found[0]

    def test_cli_and_gui_pass_the_same_kwargs(self):
        cli = self._brief_call_kwargs("scripts/week_report.py")
        gui = self._brief_call_kwargs("ffbot/webapi.py")
        assert cli == gui, (
            "week_report.py and webapi.py build the week brief differently: "
            f"only in CLI {sorted(cli - gui)}, only in GUI {sorted(gui - cli)}"
        )

    def test_opponent_starters_is_one_of_them(self):
        # Pinned by name because this is the one that actually drifted, and
        # `opponent_correlation_weight` is non-zero at every spice level, so
        # dropping it is never a no-op in practice.
        assert "opponent_starters" in self._brief_call_kwargs("scripts/week_report.py")


class TestEveryLiveSeamAlertReachesStderr:
    """`run_report` prints `_all_alerts`, not a hand-written list of seams.

    Same drift, same commit, one layer over: `opponent_alerts` was added to
    `_all_alerts` (so it reached the week log and the GUI) but never to the
    eight stderr loops that used to enumerate the seams by hand here. A
    degraded opponent-starters fetch was therefore invisible to anyone
    watching this script run -- including `scripts/autorun.py`, where stderr
    is the only place an operator would ever see it. The repo's rule is that
    every live seam degrades with a SURFACED alert; enumerating them twice is
    how "surfaced" quietly became "surfaced in two of the three places".
    """

    def test_run_report_prints_the_shared_alert_list(self):
        from pathlib import Path

        source = Path("scripts/week_report.py").read_text(encoding="utf-8")
        assert "for a in _all_alerts(loaded):" in source
        # No seam-by-seam stderr enumeration left to fall out of sync.
        for seam in ("projection_alerts", "roster_source_alerts", "league_rosters_alerts",
                     "game_conditions_alerts", "standings_alerts", "opponent_alerts",
                     "board_alerts", "scoring_alerts", "season_ptd_alerts"):
            assert source.count(f"loaded.{seam}") == 1, (
                f"loaded.{seam} is referenced more than once in week_report.py -- "
                "alerts should flow through _all_alerts only"
            )


class TestSleeperSlotsSkipLineupState:
    def _fake_client_class(self):
        class _FakeClient:
            PLAYERS = {"1": {"full_name": "Josh Allen", "position": "QB", "team": "BUF", "injury_status": None}}

            def __init__(self, cache_dir=None, force_refresh=False):
                pass

            def players(self):
                return dict(self.PLAYERS)

            def ownership(self, season, week):
                return {}

            def rosters(self, league_id, **kwargs):
                return [{"roster_id": 1, "owner_id": "u1", "players": ["1"], "starters": ["1"], "settings": {}}]

            def league(self, league_id, **kwargs):
                return {"roster_positions": ["QB", "BN", "BN"]}

            def user(self, username):
                return None

        return _FakeClient

    def test_sleeper_slots_skip_lineup_state_write(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config_path = tmp_path / "config.yml"
        config_path.write_text(
            "roster_positions:\n  QB: 1\n  RB: 1\n  BN: 2\n"
            "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
            f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
            f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
            "roster_source:\n  source: sleeper\n"
            "sleeper:\n  league_id: \"L1\"\n  roster_id: 1\n",
            encoding="utf-8",
        )
        state_path = tmp_path / "state.yml"
        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", self._fake_client_class())

        args = parse_args([
            "--config", str(config_path), "--roster", str(tmp_path / "no_roster.yml"), "--week", "1",
            "--state", str(state_path), "--league-rosters", str(tmp_path / "no_lr.yml"),
        ])
        run = run_report(args)
        assert run.loaded.slots_source == "sleeper"
        assert not state_path.exists()

    def test_file_route_still_writes_lineup_state(self, tmp_path, monkeypatch):
        # Contrast case -- the file route's existing behavior must survive
        # the slots-aware gating unchanged.
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config = _write_config(tmp_path, board_csv)
        roster = _write_roster(tmp_path)
        state_path = tmp_path / "state.yml"
        args = parse_args([
            "--config", str(config), "--roster", str(roster), "--week", "1",
            "--state", str(state_path), "--league-rosters", str(tmp_path / "no_lr.yml"),
        ])
        run = run_report(args)
        assert run.loaded.slots_source == "file"
        assert state_path.exists()

    def test_refresh_flag_reaches_load_everything(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        board_csv = _write_board_csv(tmp_path)
        config_path = tmp_path / "config.yml"
        config_path.write_text(
            "roster_positions:\n  QB: 1\n  RB: 1\n  BN: 2\n"
            "draft:\n  num_teams: 12\n  my_slot: 1\n  rounds: 6\n"
            f"  board_csv: [\"{board_csv.as_posix()}\"]\n"
            f"  intel_file: \"{(tmp_path / 'no-intel.yml').as_posix()}\"\n"
            "roster_source:\n  source: sleeper\n"
            "sleeper:\n  league_id: \"L1\"\n  roster_id: 1\n",
            encoding="utf-8",
        )
        captured: list[bool] = []
        base = self._fake_client_class()

        class _RecordingClient(base):
            def __init__(self, cache_dir=None, force_refresh=False):
                captured.append(force_refresh)
                super().__init__(cache_dir, force_refresh)

        monkeypatch.setattr("ffbot.sleeper.client.SleeperClient", _RecordingClient)

        args = parse_args([
            "--config", str(config_path), "--roster", str(tmp_path / "no_roster.yml"), "--week", "1",
            "--state", str(tmp_path / "state.yml"), "--league-rosters", str(tmp_path / "no_lr.yml"),
            "--refresh",
        ])
        run_report(args)
        assert True in captured
