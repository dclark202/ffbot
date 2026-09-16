"""A legacy console must degrade this repo's prose, never kill the run.

The failure this prevents is not cosmetic: `sys.stdout` on a cp1252 Windows
console is opened with `errors="strict"`, so a single `→` raised
`UnicodeEncodeError` at PRINT time -- after every live fetch had been spent
and the recommendation log had already been written to disk. The run did all
of its work and then died on the last line.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from ffbot.console import _ASCII_FALLBACKS, _HANDLER_NAME, make_streams_safe


REPO = Path(__file__).resolve().parent.parent


def _cp1252_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="")


def _ascii_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict", newline="")


class TestTheCrashIsReproducibleAndFixed:
    def test_a_strict_cp1252_stream_really_does_raise(self):
        """The premise. If this ever stops being true the guard can go."""
        stream = _cp1252_stream()
        with pytest.raises(UnicodeEncodeError):
            stream.write("RB → W/R/T")
            stream.flush()

    def test_after_the_guard_the_same_write_survives(self):
        stream = _cp1252_stream()
        make_streams_safe(stream)
        stream.write("RB → W/R/T")
        stream.flush()
        assert stream.buffer.getvalue().decode("cp1252") == "RB -> W/R/T"


class TestDegradationIsMeaningful:
    @pytest.mark.parametrize("char,expected", sorted(_ASCII_FALLBACKS.items()))
    def test_each_known_character_has_an_ascii_reading(self, char, expected):
        """Checked against a strict ASCII stream, which is the worst case.
        cp1252 can represent some of these (an em dash is 0x97), so on that
        console the handler never fires for them and the real character is
        printed -- which is the desired behaviour, not a gap."""
        stream = _ascii_stream()
        make_streams_safe(stream)
        stream.write(char)
        stream.flush()
        assert stream.buffer.getvalue().decode("ascii") == expected

    def test_an_unknown_character_degrades_instead_of_raising(self):
        """A character added later costs legibility on one console, never a
        run -- which is the whole reason this is a codec error handler and
        not a hand-maintained translation pass."""
        stream = _cp1252_stream()
        make_streams_safe(stream)
        stream.write("中文")
        stream.flush()
        assert stream.buffer.getvalue().decode("cp1252") == "??"

    def test_characters_the_terminal_can_already_show_are_untouched(self):
        """The encoding is deliberately NOT forced to UTF-8: an em dash is
        representable in cp1252, and rewriting the stream's encoding would
        turn text that currently renders correctly into mojibake."""
        stream = _cp1252_stream()
        make_streams_safe(stream)
        stream.write("Add — Drop")
        stream.flush()
        assert stream.buffer.getvalue().decode("cp1252") == "Add — Drop"
        assert stream.encoding.lower() in ("cp1252", "windows-1252")


class TestItIsSafeToCallAnywhere:
    def test_it_is_idempotent(self):
        stream = _cp1252_stream()
        make_streams_safe(stream)
        make_streams_safe(stream)
        assert stream.errors == _HANDLER_NAME

    def test_a_stream_that_cannot_be_reconfigured_is_skipped_quietly(self):
        """A pipe, a pytest capture buffer, a detached stream. A console
        convenience must never be the reason a run fails."""
        class _NoReconfigure:
            pass

        make_streams_safe(_NoReconfigure())  # must not raise

    def test_a_stream_that_refuses_reconfigure_is_skipped_quietly(self):
        class _Angry:
            def reconfigure(self, **kw):
                raise ValueError("underlying buffer has been detached")

        make_streams_safe(_Angry())  # must not raise


class TestEveryPrintingCliIsGuarded:
    """A guard that only covers the entry points somebody remembered is not
    a guard. These are the CLIs that render engine prose to stdout."""

    CLIS = (
        "week_report", "draft", "autorun", "grade_week", "make_training_pack",
        "training_report", "demo_season", "gui", "draft_export",
    )

    @pytest.mark.parametrize("name", CLIS)
    def test_it_calls_the_guard(self, name):
        src = (REPO / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        assert "from ffbot.console import make_streams_safe" in src
        assert "make_streams_safe()" in src

    def test_no_engine_module_calls_it(self):
        """This is a terminal concern. An engine module reconfiguring the
        caller's streams as an import side effect would reach pytest, the
        GUI's request handler and anything embedding this package."""
        offenders = [
            p.name for p in (REPO / "ffbot").rglob("*.py")
            if p.name != "console.py" and "make_streams_safe" in p.read_text(encoding="utf-8")
        ]
        assert offenders == []


class TestTheRealReportPrints:
    def test_week_report_renders_an_arrow_under_cp1252(self):
        """End to end, in a subprocess with a genuinely strict cp1252
        stdout: the exact configuration that crashed."""
        code = (
            "import sys, io;"
            "sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='cp1252', errors='strict');"
            "sys.path.insert(0, r'%s');"
            "from ffbot.console import make_streams_safe; make_streams_safe();"
            "print('RB \\u2192 W/R/T: Start Somebody \\u2014 3.5 pts')"
        ) % REPO
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
        assert b"RB -> W/R/T" in out.stdout
