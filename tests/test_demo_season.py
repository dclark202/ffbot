from __future__ import annotations

import datetime as dt

import yaml

from ffbot import report
from ffbot.board import Board, BoardPlayer
from ffbot.history.index import InjuryReportRow

import scripts.demo_season as ds


def _bp(name: str, position: str, team: str, points: float = 100.0, bye: int | None = None, adp: float | None = 10.0) -> BoardPlayer:
    key = f"{name.lower()}:{position}"
    return BoardPlayer(
        key=key, name=name, position=position, team=team, bye_week=bye, points=points,
        adp=adp, adp_stdev=2.0, adp_spread=None, platform_id=None, tier=1, vor=points - 50.0, rank=1,
    )


class TestResolveClock:
    _BOUNDS = {
        1: (dt.date(2025, 9, 4), dt.date(2025, 9, 8)),
        2: (dt.date(2025, 9, 11), dt.date(2025, 9, 15)),
        6: (dt.date(2025, 10, 9), dt.date(2025, 10, 13)),
    }

    def test_wednesday_of_the_bounded_week(self):
        assert ds.resolve_clock(dt.date(2025, 10, 8), self._BOUNDS) == (6, "wed")

    def test_friday_of_the_bounded_week(self):
        assert ds.resolve_clock(dt.date(2025, 10, 10), self._BOUNDS) == (6, "fri")

    def test_sunday_of_the_bounded_week(self):
        assert ds.resolve_clock(dt.date(2025, 10, 12), self._BOUNDS) == (6, "sun")

    def test_date_between_two_weeks_rolls_to_the_next_week(self):
        # Sept 10 is after week 1's last game (Sept 8) and before week 2's
        # first (Sept 11) -- by Wednesday the upcoming week has already
        # started, so this must resolve to week 2, not week 1.
        assert ds.resolve_clock(dt.date(2025, 9, 10), self._BOUNDS) == (2, "wed")

    def test_tuesday_and_wednesday_are_both_the_wed_state(self):
        # Both sides of the Tue/Wed boundary are still pre-report -- no
        # visible difference is expected (the official report lands Friday).
        tue = ds.resolve_clock(dt.date(2025, 10, 7), self._BOUNDS)
        wed = ds.resolve_clock(dt.date(2025, 10, 8), self._BOUNDS)
        assert tue[1] == wed[1] == "wed"
        assert tue[0] == wed[0] == 6

    def test_wed_to_fri_boundary_flips_state(self):
        wed = ds.resolve_clock(dt.date(2025, 10, 8), self._BOUNDS)  # Wed
        thu = ds.resolve_clock(dt.date(2025, 10, 9), self._BOUNDS)  # Thu
        assert wed[1] == "wed"
        assert thu[1] == "fri"

    def test_date_after_the_season_clamps_to_the_last_known_week(self):
        assert ds.resolve_clock(dt.date(2025, 12, 31), self._BOUNDS) == (6, "wed")

    def test_date_before_the_season_clamps_to_the_first_week(self):
        # July 28, 2025 is a Monday -- "wed" state, and well before week 1's
        # Sept 4 start, exercising the "before the season" clamp.
        assert ds.resolve_clock(dt.date(2025, 7, 28), self._BOUNDS) == (1, "wed")


class TestTeamByes:
    def test_team_missing_a_week_is_flagged_on_bye(self, tmp_path):
        rows = [
            {"season": "2025", "week": "1", "game_type": "REG", "home_team": "BUF", "away_team": "MIA"},
            {"season": "2025", "week": "2", "game_type": "REG", "home_team": "BUF", "away_team": "NYJ"},
            # MIA has no week-2 game -- on bye.
            {"season": "2025", "week": "2", "game_type": "REG", "home_team": "NE", "away_team": "PHI"},
        ]

        def opener(url: str) -> bytes:
            header = "season,week,game_type,home_team,away_team\n"
            body = "".join(f"{r['season']},{r['week']},{r['game_type']},{r['home_team']},{r['away_team']}\n" for r in rows)
            return (header + body).encode("utf-8")

        byes = ds._team_byes(2025, tmp_path, opener)
        assert byes["MIA"] == 2
        assert "BUF" not in byes  # played every week in the fixture


class TestWriteBoardCsvs:
    def test_round_trips_through_read_fantasypros_with_byes_intact(self, tmp_path):
        board = Board(players=[
            _bp("Josh Allen", "QB", "BUF", points=320.5),
            _bp("Some Guy", "WR", "MIA", points=110.0),  # no bye resolved for MIA -- stays blank
        ])
        stats = ds._write_board_csvs(board, byes={"BUF": 12}, board_dir=tmp_path / "board")
        assert stats == {"players": 2, "with_bye": 1}

    def test_bye_backfill_wins_over_missing_board_bye(self, tmp_path):
        from ffbot.board import read_fantasypros

        board = Board(players=[_bp("Josh Allen", "QB", "BUF", points=320.5, bye=None)])
        ds._write_board_csvs(board, byes={"BUF": 12}, board_dir=tmp_path / "board")

        rows = read_fantasypros(tmp_path / "board" / "projections.csv")
        assert rows[0]["name"] == "Josh Allen"
        assert rows[0]["position"] == "QB"
        assert rows[0]["bye"] == 12
        assert rows[0]["points"] == 320.5

    def test_adp_csv_carries_adp_and_stdev(self, tmp_path):
        from ffbot.board import read_fantasypros

        board = Board(players=[_bp("Josh Allen", "QB", "BUF", adp=5.5)])
        ds._write_board_csvs(board, byes={}, board_dir=tmp_path / "board")

        rows = read_fantasypros(tmp_path / "board" / "adp.csv")
        assert rows[0]["adp"] == 5.5
        assert rows[0]["adp_stdev"] == 2.0

    def test_player_with_no_resolved_bye_writes_blank_not_a_crash(self, tmp_path):
        from ffbot.board import read_fantasypros

        board = Board(players=[_bp("Mystery Guy", "TE", "ZZZ", bye=None)])
        stats = ds._write_board_csvs(board, byes={}, board_dir=tmp_path / "board")
        assert stats["with_bye"] == 0

        rows = read_fantasypros(tmp_path / "board" / "projections.csv")
        assert rows[0]["bye"] is None


class TestNoteSynthesis:
    def _row(self, **kw) -> InjuryReportRow:
        defaults = dict(name="Real Player", team="SF", report_status="", practice_status="", injury="")
        defaults.update(kw)
        return InjuryReportRow(**defaults)

    def test_practice_note_reflects_participation_level(self):
        assert "did not practice" in ds._practice_note(self._row(practice_status="Did Not Participate In Practice", injury="Hamstring"))
        assert "limited" in ds._practice_note(self._row(practice_status="Limited Participation in Practice"))
        assert "fully" in ds._practice_note(self._row(practice_status="Full Participation in Practice"))

    def test_report_note_only_present_with_a_real_designation(self):
        assert ds._report_note(self._row(report_status="")) == ""
        note = ds._report_note(self._row(report_status="Questionable", injury="Ankle"))
        assert "questionable" in note
        assert "ankle" in note

    def test_hindsight_note_requires_questionable_status(self):
        row = self._row(report_status="Out")
        assert ds._hindsight_note(row, actuals={}, position="WR") == ""

    def test_hindsight_note_flags_missing_box_score_line(self):
        row = self._row(name="Real Player", report_status="Questionable")
        assert "did not appear" in ds._hindsight_note(row, actuals={}, position="WR")

    def test_hindsight_note_flags_played_despite_tag(self):
        from ffbot.history.names import actuals_key

        row = self._row(name="Real Player", report_status="Questionable")
        actuals = {actuals_key("Real Player", "WR"): 12.0}
        assert "played despite" in ds._hindsight_note(row, actuals=actuals, position="WR")


class TestWriteDemoConfig:
    """The demo's config.local.yml must always force projection_source to
    "board", regardless of what the real repo's own config(.local).yml
    says -- "sleeper" fetches the CURRENT NFL season, which would be wrong
    (or worse, silently plausible-looking-but-wrong) for a demo that
    replays a specific past season."""

    def _fake_repo(self, tmp_path, real_local_overrides: dict | None = None):
        repo = tmp_path / "fake_repo"
        repo.mkdir()
        (repo / "config.yml").write_text("league_id: ''\n", encoding="utf-8")
        if real_local_overrides is not None:
            (repo / "config.local.yml").write_text(yaml.safe_dump(real_local_overrides), encoding="utf-8")
        return repo

    def test_forces_board_even_when_the_real_repo_uses_sleeper(self, tmp_path, monkeypatch):
        repo = self._fake_repo(tmp_path, {"projection_source": {"source": "sleeper", "cache_ttl_minutes": 30}})
        monkeypatch.setattr(ds, "REPO_ROOT", repo)

        demo_dir = tmp_path / "demo_out"
        demo_dir.mkdir()
        ds._write_demo_config(demo_dir)

        written = yaml.safe_load((demo_dir / "config.local.yml").read_text(encoding="utf-8"))
        assert written["projection_source"]["source"] == "board"

    def test_defaults_to_board_when_the_real_repo_has_no_local_override(self, tmp_path, monkeypatch):
        repo = self._fake_repo(tmp_path, real_local_overrides=None)
        monkeypatch.setattr(ds, "REPO_ROOT", repo)

        demo_dir = tmp_path / "demo_out"
        demo_dir.mkdir()
        ds._write_demo_config(demo_dir)

        written = yaml.safe_load((demo_dir / "config.local.yml").read_text(encoding="utf-8"))
        assert written["projection_source"]["source"] == "board"

    def test_other_real_local_overrides_still_pass_through(self, tmp_path, monkeypatch):
        # The override is additive, not a wholesale replacement of the real
        # config.local.yml's contents.
        repo = self._fake_repo(tmp_path, {"season": {"spice_level": 4}})
        monkeypatch.setattr(ds, "REPO_ROOT", repo)

        demo_dir = tmp_path / "demo_out"
        demo_dir.mkdir()
        ds._write_demo_config(demo_dir)

        written = yaml.safe_load((demo_dir / "config.local.yml").read_text(encoding="utf-8"))
        assert written["season"]["spice_level"] == 4
        assert written["projection_source"]["source"] == "board"

    def test_forces_roster_and_standings_and_conditions_to_file_off_off(self, tmp_path, monkeypatch):
        # Every live-data switch (Phase 1-5 wiring) must also be forced off
        # for the demo, the same reasoning already applied to
        # projection_source -- a live fetch against a real current league
        # would be wrong (or worse, silently plausible) for a replay of a
        # specific past season.
        repo = self._fake_repo(
            tmp_path,
            {
                "roster_source": {"source": "sleeper"},
                "standings_source": {"source": "sleeper"},
                "league_rosters_source": {"source": "sleeper"},
                "game_conditions": {"weather_source": "open_meteo", "odds_source": "kalshi"},
                "notify": {"channel": "ntfy", "ntfy_topic": "real-topic"},
            },
        )
        monkeypatch.setattr(ds, "REPO_ROOT", repo)

        demo_dir = tmp_path / "demo_out"
        demo_dir.mkdir()
        ds._write_demo_config(demo_dir)

        written = yaml.safe_load((demo_dir / "config.local.yml").read_text(encoding="utf-8"))
        assert written["roster_source"]["source"] == "file"
        assert written["standings_source"]["source"] == "file"
        assert written["league_rosters_source"]["source"] == "file"
        assert written["game_conditions"]["weather_source"] == "off"
        assert written["game_conditions"]["odds_source"] == "off"
        assert written["notify"]["channel"] == "off"

    def test_forces_kalshi_weight_off_even_when_the_real_repo_uses_spice_level_four(self, tmp_path, monkeypatch):
        # kalshi_weight is gated to spice_level 4 (B7 -- was level 5 pre-
        # rescale), and unlike weather/odds it is NOT covered by
        # game_conditions being off -- the weekly Kalshi signal fetch in
        # ffbot/report.py runs independently of that switch. A future
        # session bumping the real repo to spice_level 4 must not make
        # every demo run reach out to the CURRENT actual week's live
        # markets while replaying a past season.
        repo = self._fake_repo(
            tmp_path,
            {"season": {"spice_level": 4}, "draft": {"spice_level": 4}},
        )
        monkeypatch.setattr(ds, "REPO_ROOT", repo)

        demo_dir = tmp_path / "demo_out"
        demo_dir.mkdir()
        ds._write_demo_config(demo_dir)

        written = yaml.safe_load((demo_dir / "config.local.yml").read_text(encoding="utf-8"))
        assert written["season"]["spice_level"] == 4  # untouched -- only kalshi_weight is forced
        assert written["season"]["kalshi_weight"] == 0.0
        assert written["draft"]["spice_level"] == 4
        assert written["draft"]["kalshi_weight"] == 0.0


class TestApplyClock:
    def _seed_demo_dir(self, tmp_path):
        demo_dir = tmp_path / "2025"
        (demo_dir / "weekly" / "variants").mkdir(parents=True)
        (demo_dir / "standings").mkdir(parents=True)

        for wk in ("01", "02"):
            for state in ("wed", "fri", "sun"):
                content = {"week": int(wk), "generated": "", "source_notes": f"week{wk}-{state}", "players": {}, "matchups": []}
                (demo_dir / "weekly" / "variants" / f"week-{wk}-{state}.yml").write_text(yaml.safe_dump(content), encoding="utf-8")

        (demo_dir / "league.base.yml").write_text(yaml.safe_dump({"name": "Test League", "games_per_season": 17}), encoding="utf-8")
        (demo_dir / "standings" / "week-01.yml").write_text(
            yaml.safe_dump({"teams": [{"name": "team_1", "seed": 1}], "my_opponent": "team_3"}), encoding="utf-8",
        )
        (demo_dir / "demo_meta.yml").write_text(yaml.safe_dump({"season": 2025, "agent_slot": 1, "num_teams": 12, "seed": 11, "max_week": ds._MAX_WEEK}), encoding="utf-8")
        return demo_dir

    def test_current_week_uses_the_requested_day_state(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="fri", allow_hindsight=False)
        raw = yaml.safe_load((demo_dir / "weekly" / "week-01.yml").read_text(encoding="utf-8"))
        assert raw["source_notes"] == "week01-fri"

    def test_future_week_is_written_blank_not_missing(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="wed", allow_hindsight=False)
        dest = demo_dir / "weekly" / "week-02.yml"
        assert dest.exists()
        raw = yaml.safe_load(dest.read_text(encoding="utf-8"))
        assert raw["week"] == 2
        assert raw["players"] == {}
        assert raw["games"] == {}

    def test_sunday_without_hindsight_clamps_to_friday_state(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="sun", allow_hindsight=False)
        raw = yaml.safe_load((demo_dir / "weekly" / "week-01.yml").read_text(encoding="utf-8"))
        assert raw["source_notes"] == "week01-fri"

    def test_sunday_with_hindsight_uses_sun_state(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="sun", allow_hindsight=True)
        raw = yaml.safe_load((demo_dir / "weekly" / "week-01.yml").read_text(encoding="utf-8"))
        assert raw["source_notes"] == "week01-sun"

    def test_league_yml_stamped_with_standings_and_opponent(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="wed", allow_hindsight=False)
        league = yaml.safe_load((demo_dir / "league.yml").read_text(encoding="utf-8"))
        assert league["week"] == 1
        assert league["my_team"] == "team_1"
        assert league["my_opponent"] == "team_3"
        assert league["teams"] == [{"name": "team_1", "seed": 1}]
        assert league["name"] == "Test League"  # base scoring survives the merge

    def test_every_week_up_to_max_week_gets_a_file(self, tmp_path):
        demo_dir = self._seed_demo_dir(tmp_path)
        ds._apply_clock(demo_dir, week=1, day_state="wed", allow_hindsight=False)
        for wk in range(1, ds._MAX_WEEK + 1):
            assert (demo_dir / "weekly" / f"week-{wk:02d}.yml").exists()


class TestBuiltDemoWeekLoadsCleanly:
    """The real acceptance check for the file shapes `demo_season.py`
    writes: does `ffbot.report.load_everything` -- the same loader both the
    CLI and the GUI use -- accept them with no `ReportError`? Builds a tiny
    demo environment by hand, through the SAME writer functions `build`
    calls (`_write_board_csvs`, `ffbot.roster_editor`, `ffbot.weekly_editor`),
    entirely offline.
    """

    def test_load_everything_succeeds_on_a_hand_built_demo_week(self, tmp_path):
        from ffbot import roster_editor, weekly_editor

        board = Board(players=[
            _bp("Josh Allen", "QB", "BUF", points=320.5, bye=12),
            _bp("Some Guy", "WR", "MIA", points=110.0, bye=8),
        ])
        board_dir = tmp_path / "board"
        ds._write_board_csvs(board, byes={}, board_dir=board_dir)

        roster_editor.write_roster_entries(str(tmp_path / "roster.yml"), [{"name": "Josh Allen"}])

        weekly_editor.write_weekly_intel(
            tmp_path / "weekly" / "week-01.yml",
            {"week": 1, "players": {"Josh Allen": {"status": "Q", "note": "limited in practice"}}},
        )

        config_text = (
            "draft:\n"
            f"  board_csv:\n    - {board_dir / 'projections.csv'}\n    - {board_dir / 'adp.csv'}\n"
            # Point away from the real repo's draft/intel.yml (which is
            # CWD-relative and would otherwise load real, unrelated intel
            # notes against this tiny 2-player fixture board) -- a
            # nonexistent path is the documented missing-file no-op.
            f"  intel_file: {tmp_path / 'nonexistent-intel.yml'}\n"
        )
        (tmp_path / "config.yml").write_text(config_text, encoding="utf-8")

        loaded = report.load_everything(
            config_path=str(tmp_path / "config.yml"),
            roster_path=str(tmp_path / "roster.yml"),
            week_num=1,
            weekly_path=str(tmp_path / "weekly" / "week-01.yml"),
            league_rosters_path=str(tmp_path / "league_rosters.yml"),  # missing -- inert
        )

        assert loaded.board is not None
        assert len(loaded.players) == 1
        assert loaded.players[0].name == "Josh Allen"
        assert loaded.weekly.players["josh allen"].status == "Q"
