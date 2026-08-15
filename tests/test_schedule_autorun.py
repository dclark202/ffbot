from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts import schedule_autorun as sa


def _fake_ok_runner(calls: list):
    def runner(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="Status: Ready", stderr="")
    return runner


def _raising_runner(calls: list):
    def runner(cmd):
        calls.append(cmd)
        raise AssertionError(f"runner should never be called, got {cmd!r}")
    return runner


class TestResolvePython:
    def test_explicit_override_wins(self):
        assert sa.resolve_python("C:/some/python.exe") == Path("C:/some/python.exe")

    def test_prefers_pythonw_sibling_when_present(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python.exe"
        fake_python.write_text("", encoding="utf-8")
        (tmp_path / "pythonw.exe").write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "executable", str(fake_python))
        assert sa.resolve_python(None) == tmp_path / "pythonw.exe"

    def test_falls_back_to_sys_executable_without_pythonw(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "python.exe"
        fake_python.write_text("", encoding="utf-8")
        monkeypatch.setattr(sys, "executable", str(fake_python))
        assert sa.resolve_python(None) == fake_python


class TestCommandBuilders:
    def test_register_command_shape(self):
        cmd = sa.build_register_command("ffbot-autorun", 15, Path("C:/venv/pythonw.exe"))
        assert cmd[:5] == ["schtasks", "/Create", "/F", "/TN", "ffbot-autorun"]
        assert "/SC" in cmd and cmd[cmd.index("/SC") + 1] == "MINUTE"
        assert "/MO" in cmd and cmd[cmd.index("/MO") + 1] == "15"
        tr = cmd[cmd.index("/TR") + 1]
        assert str(sa.AUTORUN_SCRIPT) in tr
        assert "--chdir" in tr
        assert str(sa.REPO_ROOT) in tr

    def test_register_command_every_is_honored(self):
        cmd = sa.build_register_command("ffbot-autorun", 30, Path("python.exe"))
        assert cmd[cmd.index("/MO") + 1] == "30"

    def test_extra_args_land_inside_tr(self):
        cmd = sa.build_register_command(
            "ffbot-autorun", 15, Path("python.exe"), extra_args=["--waiver-weekday", "wed"],
        )
        tr = cmd[cmd.index("/TR") + 1]
        assert "--waiver-weekday" in tr and "wed" in tr

    def test_remove_command_shape(self):
        assert sa.build_remove_command("ffbot-autorun") == ["schtasks", "/Delete", "/TN", "ffbot-autorun", "/F"]

    def test_status_command_shape(self):
        cmd = sa.build_status_command("ffbot-autorun")
        assert cmd == ["schtasks", "/Query", "/TN", "ffbot-autorun", "/V", "/FO", "LIST"]

    def test_tr_length_guard_raises_on_absurdly_long_path(self, monkeypatch):
        monkeypatch.setattr(sa, "REPO_ROOT", Path("C:/" + "x" * 400))
        with pytest.raises(ValueError, match="over schtasks"):
            sa.build_register_command("ffbot-autorun", 15, Path("python.exe"))

    def test_cron_line_shape(self):
        line = sa.cron_line(15, Path("/usr/bin/python3"))
        assert line.startswith("*/15 * * * *")
        assert "scripts/autorun.py" in line
        assert str(sa.REPO_ROOT) in line

    def test_cron_line_includes_extra_args(self):
        line = sa.cron_line(15, Path("/usr/bin/python3"), extra_args=["--no-waivers"])
        assert "--no-waivers" in line


class TestDryRunNeverInvokesTheRunner:
    def test_register_dry_run_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["register", "--dry-run"])
        rc = sa.cmd_register(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []
        out = capsys.readouterr().out
        assert "schtasks" in out and "crontab -e" in out

    def test_remove_dry_run_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["remove", "--dry-run"])
        rc = sa.cmd_remove(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []

    def test_status_dry_run_windows(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["status", "--dry-run"])
        rc = sa.cmd_status(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []


class TestNonWindowsNeverInvokesSchtasks:
    def test_register_prints_cron_equivalent(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: False)
        calls = []
        args = sa.parse_args(["register"])
        rc = sa.cmd_register(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []
        assert "crontab -e" in capsys.readouterr().out

    def test_remove_prints_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: False)
        calls = []
        args = sa.parse_args(["remove"])
        rc = sa.cmd_remove(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []
        assert "crontab" in capsys.readouterr().out

    def test_status_prints_guidance(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: False)
        calls = []
        args = sa.parse_args(["status"])
        rc = sa.cmd_status(args, _raising_runner(calls))
        assert rc == 0
        assert calls == []
        assert "crontab" in capsys.readouterr().out


class TestRegisterExecutesOnWindows:
    def test_success_prints_confirmation_and_next_steps(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["register", "--every", "10"])
        rc = sa.cmd_register(args, _fake_ok_runner(calls))
        assert rc == 0
        assert len(calls) == 1
        assert calls[0][:2] == ["schtasks", "/Create"]
        out = capsys.readouterr().out
        assert "Registered" in out
        assert "config.local.yml" in out  # the notify-setup pointer

    def test_schtasks_failure_is_reported_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)

        def failing_runner(cmd):
            return SimpleNamespace(returncode=1, stdout="", stderr="access denied")

        args = sa.parse_args(["register"])
        rc = sa.cmd_register(args, failing_runner)
        assert rc == 1
        assert "access denied" in capsys.readouterr().err

    def test_extra_args_after_double_dash_are_forwarded(self, monkeypatch):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["register", "--", "--waiver-weekday", "wed", "--waiver-hour", "21"])
        sa.cmd_register(args, _fake_ok_runner(calls))
        tr = calls[0][calls[0].index("/TR") + 1]
        assert "--waiver-weekday" in tr and "wed" in tr and "--waiver-hour" in tr and "21" in tr


class TestRemoveAndStatusExecuteOnWindows:
    def test_remove_success(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["remove"])
        rc = sa.cmd_remove(args, _fake_ok_runner(calls))
        assert rc == 0
        assert "Removed" in capsys.readouterr().out

    def test_remove_when_not_registered_is_not_an_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)

        def not_found_runner(cmd):
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: not found")

        args = sa.parse_args(["remove"])
        rc = sa.cmd_remove(args, not_found_runner)
        assert rc == 0
        assert "not registered" in capsys.readouterr().out

    def test_status_prints_query_output(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)
        calls = []
        args = sa.parse_args(["status"])
        rc = sa.cmd_status(args, _fake_ok_runner(calls))
        assert rc == 0
        assert "Status: Ready" in capsys.readouterr().out

    def test_status_when_not_registered(self, monkeypatch, capsys):
        monkeypatch.setattr(sa, "is_windows", lambda: True)

        def not_found_runner(cmd):
            return SimpleNamespace(returncode=1, stdout="", stderr="")

        args = sa.parse_args(["status"])
        rc = sa.cmd_status(args, not_found_runner)
        assert rc == 0
        assert "not registered" in capsys.readouterr().out


class TestMainNeverUsesShellTrue:
    def test_default_runner_never_passes_shell_true(self, monkeypatch):
        captured = {}
        real_run = subprocess.run

        def spy(cmd, **kwargs):
            captured["shell"] = kwargs.get("shell", False)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", spy)
        sa._default_runner(["schtasks", "/Query", "/TN", "nope"])
        assert captured["shell"] is False
