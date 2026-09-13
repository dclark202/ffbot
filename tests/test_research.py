"""Unattended research (`ffbot/research.py`) -- the one seam that lets a model
with web access write a file that moves numbers, with nobody reviewing it.
These prove the guardrails structurally: the tool boundary, the
official-source rule, rollback, and that a failure never takes down a check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from ffbot import research, week
from ffbot.config import OFFICIAL_SOURCE_DOMAINS, ResearchConfig

COMMAND = "---\ndescription: test\n---\nResearch the week.\n\n$ARGUMENTS\n"


class _Proc:
    def __init__(self, returncode=0, stdout=b"Updated 1 player.", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def env(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")
    cfg = ResearchConfig(enabled=True, claude_path=str(exe))
    return cfg, tmp_path / "weekly" / "week-02.yml", tmp_path


def _ctx(mode="slot"):
    slot = mode == "slot"
    return research.ResearchContext(
        season=2026, week=2, mode=mode,
        kickoff_et="2026-09-13T13:00" if slot else "",
        slot_teams=("SEA", "NE") if slot else (),
        roster=("Jaxon Smith-Njigba (WR, SEA, starter, Sleeper status: -)",),
        candidates=("Kansas City Chiefs (DEF, KC)",),
    )


def _file(players=None, games=None):
    return yaml.safe_dump(
        {"week": 2, "generated": "2026-09-13T11:05", "source_notes": "nfl.com",
         "players": players or {}, "games": games or {}},
        sort_keys=False,
    )


def _writes(week_path, content, proc=None, calls=None):
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        if content is not None:
            week_path.write_text(content, encoding="utf-8")
        return proc or _Proc()

    return run


def _run(env, content=None, *, mode="slot", proc=None, calls=None, runner=None, prior=None):
    cfg, week_path, root = env
    if prior is not None:
        week_path.parent.mkdir(parents=True, exist_ok=True)
        week_path.write_text(prior, encoding="utf-8")
    return research.run_research(
        _ctx(mode), cfg, week_path, repo_root=root,
        runner=runner or _writes(week_path, content, proc, calls), command_text=COMMAND,
    )


def _player(env, name):
    intel = week.load_weekly_intel(env[1])
    return next(p for p in intel.players.values() if p.name == name)


class TestOfficialSourceRule:
    """A researched status overrides Sleeper's live one, so a hallucinated or
    planted "O" would silently bench a starter. Only official sources count."""

    def test_status_from_nfl_com_is_kept(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {
            "status": "O", "source": "https://www.nfl.com/injuries/league/2026/REG2", "note": "hamstring",
        }}))
        assert r.ok, r.alerts
        assert r.overrides == ["Jaxon Smith-Njigba: O (nfl.com)"]
        assert _player(env, "Jaxon Smith-Njigba").status == "O"

    def test_status_from_a_team_site_is_kept(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {
            "status": "Q", "source": "https://www.seahawks.com/news/injury-report-week-2",
        }}))
        assert r.overrides == ["Jaxon Smith-Njigba: Q (seahawks.com)"]

    def test_status_from_an_unofficial_site_becomes_a_note(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {
            "status": "O", "source": "https://x.com/somebeatwriter/status/1", "note": "hamstring",
        }}))
        assert r.ok
        assert r.overrides == []
        assert r.downgraded == ["Jaxon Smith-Njigba: O (https://x.com/somebeatwriter/status/1)"]
        p = _player(env, "Jaxon Smith-Njigba")
        assert p.status == ""
        assert p.note.startswith("unverified status O") and "hamstring" in p.note

    def test_a_lookalike_domain_is_not_official(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {
            "status": "O", "source": "https://nfl.com.injury-news.example/a",
        }}))
        assert r.overrides == [] and len(r.downgraded) == 1

    def test_a_status_with_no_source_becomes_a_note(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {"status": "D"}}))
        assert r.downgraded == ["Jaxon Smith-Njigba: D (no source)"]
        assert _player(env, "Jaxon Smith-Njigba").status == ""

    def test_a_source_without_a_scheme_is_not_official(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {"status": "O", "source": "nfl.com/injuries"}}))
        assert r.overrides == [] and len(r.downgraded) == 1

    def test_notes_without_a_status_are_untouched(self, env):
        r = _run(env, _file({"Jaxon Smith-Njigba": {"note": "trending toward playing"}}))
        assert r.ok and r.downgraded == [] and r.overrides == []
        assert _player(env, "Jaxon Smith-Njigba").note == "trending toward playing"


PRIOR = _file({"Someone": {"note": "Friday research"}})


class TestRollback:
    """A week file that no longer loads would cost `ffbot.report` the whole
    check. Every failure leaves the file exactly as it was."""

    def test_not_logged_in_rolls_back_and_says_how_to_fix(self, env):
        r = _run(env, None, prior=PRIOR, proc=_Proc(stdout="Not logged in · Please run /login".encode("utf-8")))
        assert not r.ok
        assert "/login" in r.alerts[0]
        assert env[1].read_text(encoding="utf-8") == PRIOR

    def test_a_timeout_rolls_back(self, env):
        cfg, week_path, root = env

        def run(argv, **kw):
            week_path.write_text("garbage: [", encoding="utf-8")
            raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

        r = _run(env, runner=run, prior=PRIOR)
        assert not r.ok and "timed out" in r.alerts[0]
        assert week_path.read_text(encoding="utf-8") == PRIOR

    def test_an_unreadable_file_is_rolled_back(self, env):
        r = _run(env, "players: [not, a, mapping]\n", prior=PRIOR)
        assert not r.ok and "unreadable" in r.alerts[0]
        assert env[1].read_text(encoding="utf-8") == PRIOR

    def test_a_failed_first_run_leaves_no_file_behind(self, env):
        """No prior file means "no research" -- a missing file is the inert
        state, so a failure must not leave a half-written or skeleton file."""
        r = _run(env, "players: [broken\n")
        assert not r.ok
        assert not env[1].exists()

    def test_a_nonzero_exit_rolls_back(self, env):
        r = _run(env, _file({"A": {"note": "x"}}), prior=PRIOR, proc=_Proc(returncode=2, stderr=b"boom"))
        assert not r.ok and "exit 2" in r.alerts[0]
        assert env[1].read_text(encoding="utf-8") == PRIOR

    def test_a_run_that_changes_nothing_says_so(self, env):
        r = _run(env, None, prior=PRIOR)
        assert not r.ok and "without updating" in r.alerts[0]
        assert env[1].read_text(encoding="utf-8") == PRIOR

    @pytest.mark.parametrize("exc", [OSError("disk"), RuntimeError("boom"), ValueError("bad")])
    def test_never_raises_whatever_the_runner_does(self, env, exc):
        cfg, week_path, root = env

        def run(argv, **kw):
            week_path.write_text("garbage: [", encoding="utf-8")
            raise exc

        r = _run(env, runner=run, prior=PRIOR)
        assert not r.ok and r.alerts
        assert week_path.read_text(encoding="utf-8") == PRIOR


class TestSlotRunsNeverDelete:
    def test_a_slot_run_restores_entries_it_dropped(self, env):
        prior = _file({"Friday Guy": {"note": "Friday research"}, "Slot Guy": {"note": "old"}})
        r = _run(env, _file({"Slot Guy": {"note": "active, per inactives"}}), prior=prior)
        assert r.ok
        assert _player(env, "Friday Guy").note == "Friday research"
        assert _player(env, "Slot Guy").note == "active, per inactives"

    def test_a_full_run_may_prune_resolved_entries(self, env):
        prior = _file({"Resolved Guy": {"note": "was questionable"}, "Current Guy": {"note": "old"}})
        r = _run(env, _file({"Current Guy": {"note": "new"}}), mode="full", prior=prior)
        assert r.ok
        names = {p.name for p in week.load_weekly_intel(env[1]).players.values()}
        assert names == {"Current Guy"}


class TestLockedDown:
    def test_a_missing_cli_skips_without_running_anything(self, env):
        cfg, week_path, root = env

        def exploding(argv, **kw):
            raise AssertionError("must not run without a CLI")

        missing = ResearchConfig(enabled=True, claude_path=str(root / "nope.exe"))
        r = research.run_research(_ctx(), missing, week_path, repo_root=root, runner=exploding, command_text=COMMAND)
        assert not r.ok and "not found" in r.alerts[0]
        assert not week_path.exists()

    def test_argv_allows_only_the_web_and_the_one_week_file(self, env):
        calls = []
        _run(env, _file({"A": {"note": "x"}}), calls=calls)
        argv, _ = calls[0]
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert "Edit(weekly/week-02.yml)" in argv and "Write(weekly/week-02.yml)" in argv
        assert "Edit" not in argv and "Write" not in argv  # never unscoped
        assert argv[argv.index("--disallowedTools") + 1] == "Bash"
        assert "--strict-mcp-config" in argv
        assert not any("bypass" in a.lower() or "dangerously" in a.lower() for a in argv)

    def test_the_prompt_carries_the_context_not_a_placeholder(self, env):
        calls = []
        _run(env, _file({"A": {"note": "x"}}), calls=calls)
        prompt = calls[0][1]["input"].decode("utf-8")
        assert "week: 2" in prompt and "file: weekly/week-02.yml" in prompt
        assert "slot kickoff (ET): 2026-09-13T13:00" in prompt
        assert "Jaxon Smith-Njigba" in prompt
        assert "$ARGUMENTS" not in prompt and "description:" not in prompt

    @pytest.mark.parametrize("mode", ["slot", "full"])
    def test_the_timeout_is_the_modes_budget(self, env, mode):
        cfg = env[0]
        calls = []
        _run(env, _file({"A": {"note": "x"}}), mode=mode, calls=calls)
        budget = cfg.slot_timeout_minutes if mode == "slot" else cfg.full_timeout_minutes
        assert calls[0][1]["timeout"] == budget * 60

    def test_runs_from_the_repo_root(self, env):
        calls = []
        _run(env, _file({"A": {"note": "x"}}), calls=calls)
        assert calls[0][1]["cwd"] == str(env[2])


class TestConfig:
    def test_off_by_default(self):
        assert ResearchConfig().enabled is False

    def test_official_domains_are_the_league_and_all_32_teams(self):
        assert "nfl.com" in OFFICIAL_SOURCE_DOMAINS
        assert len(OFFICIAL_SOURCE_DOMAINS) == len(set(OFFICIAL_SOURCE_DOMAINS)) == 33

    def test_the_shipped_command_file_takes_arguments(self):
        text = (Path(__file__).resolve().parent.parent / research.COMMAND_FILE).read_text(encoding="utf-8")
        assert "$ARGUMENTS" in text
