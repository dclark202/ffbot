from __future__ import annotations

import subprocess

import pytest

from ffbot.config import NotifyConfig
from ffbot.notify import send


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


class TestOffChannel:
    def test_off_calls_neither_runner_nor_opener(self):
        def exploding_opener(req):
            raise AssertionError("must not be called when channel is off")

        def exploding_runner(args, **kwargs):
            raise AssertionError("must not be called when channel is off")

        cfg = NotifyConfig(channel="off")
        alerts = send(cfg, "title", "body", runner=exploding_runner, opener=exploding_opener)
        assert alerts == []

    def test_off_is_the_dataclass_default(self):
        assert NotifyConfig().channel == "off"


class TestNtfyChannel:
    def test_posts_to_server_topic_with_title_query_param(self):
        calls = []

        def opener(req):
            calls.append(req)
            return b""

        cfg = NotifyConfig(channel="ntfy", ntfy_server="https://ntfy.sh", ntfy_topic="my-topic")
        alerts = send(cfg, "ffbot W3", "Lineup: 2 moves", opener=opener)

        assert alerts == []
        assert len(calls) == 1
        req = calls[0]
        assert req.full_url == "https://ntfy.sh/my-topic?title=ffbot%20W3"
        assert req.data == b"Lineup: 2 moves"
        assert req.get_method() == "POST"

    def test_trailing_slash_on_server_is_stripped(self):
        calls = []

        def opener(req):
            calls.append(req.full_url)
            return b""

        cfg = NotifyConfig(channel="ntfy", ntfy_server="https://ntfy.sh/", ntfy_topic="t")
        send(cfg, "title", "body", opener=opener)
        assert calls[0].startswith("https://ntfy.sh/t?")

    def test_blank_topic_is_an_alert_with_no_network_call(self):
        def exploding_opener(req):
            raise AssertionError("must not be called with no topic configured")

        cfg = NotifyConfig(channel="ntfy", ntfy_topic="")
        alerts = send(cfg, "title", "body", opener=exploding_opener)
        assert len(alerts) == 1
        assert "topic" in alerts[0].lower()

    def test_whitespace_only_topic_is_treated_as_blank(self):
        def exploding_opener(req):
            raise AssertionError("must not be called")

        cfg = NotifyConfig(channel="ntfy", ntfy_topic="   ")
        alerts = send(cfg, "title", "body", opener=exploding_opener)
        assert len(alerts) == 1

    def test_raising_opener_returns_an_alert_never_raises(self):
        import urllib.error

        def raising_opener(req):
            raise urllib.error.URLError("simulated network failure")

        cfg = NotifyConfig(channel="ntfy", ntfy_topic="t")
        alerts = send(cfg, "title", "body", opener=raising_opener)
        assert len(alerts) == 1
        assert "ntfy" in alerts[0].lower()

    def test_non_ascii_title_does_not_raise(self):
        # The whole reason title travels as a query param, not a header --
        # header values must be latin-1 encodable and a player/report title
        # need not be.
        calls = []

        def opener(req):
            calls.append(req.full_url)
            return b""

        cfg = NotifyConfig(channel="ntfy", ntfy_topic="t")
        alerts = send(cfg, "José Guy — W3", "body", opener=opener)
        assert alerts == []
        assert len(calls) == 1


class TestToastChannel:
    def test_runner_invoked_with_powershell(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return _FakeCompletedProcess(returncode=0)

        cfg = NotifyConfig(channel="toast")
        alerts = send(cfg, "ffbot W3", "Lineup: 2 moves", runner=runner)

        assert alerts == []
        assert len(calls) == 1
        assert calls[0][0] == "powershell"
        script = calls[0][-1]
        assert "ffbot W3" in script
        assert "Lineup: 2 moves" in script

    def test_single_quotes_in_title_are_escaped_not_broken_out_of(self):
        calls = []

        def runner(args, **kwargs):
            calls.append(args)
            return _FakeCompletedProcess(returncode=0)

        cfg = NotifyConfig(channel="toast")
        send(cfg, "Bo's Team W3", "body", runner=runner)
        script = calls[0][-1]
        assert "Bo''s Team W3" in script  # PowerShell single-quote escaping

    def test_nonzero_exit_is_an_alert(self):
        def runner(args, **kwargs):
            return _FakeCompletedProcess(returncode=1, stderr=b"boom")

        cfg = NotifyConfig(channel="toast")
        alerts = send(cfg, "title", "body", runner=runner)
        assert len(alerts) == 1
        assert "toast" in alerts[0].lower()
        assert "boom" in alerts[0]

    def test_raising_runner_returns_an_alert_never_raises(self):
        def raising_runner(args, **kwargs):
            raise subprocess.SubprocessError("simulated failure")

        cfg = NotifyConfig(channel="toast")
        alerts = send(cfg, "title", "body", runner=raising_runner)
        assert len(alerts) == 1
        assert "toast" in alerts[0].lower()


class TestBothChannel:
    def test_fans_out_to_ntfy_and_toast(self):
        opener_calls, runner_calls = [], []

        def opener(req):
            opener_calls.append(req)
            return b""

        def runner(args, **kwargs):
            runner_calls.append(args)
            return _FakeCompletedProcess(returncode=0)

        cfg = NotifyConfig(channel="both", ntfy_topic="t")
        alerts = send(cfg, "title", "body", opener=opener, runner=runner)

        assert alerts == []
        assert len(opener_calls) == 1
        assert len(runner_calls) == 1

    def test_one_failing_channel_does_not_block_the_other(self):
        import urllib.error

        def raising_opener(req):
            raise urllib.error.URLError("simulated network failure")

        runner_calls = []

        def runner(args, **kwargs):
            runner_calls.append(args)
            return _FakeCompletedProcess(returncode=0)

        cfg = NotifyConfig(channel="both", ntfy_topic="t")
        alerts = send(cfg, "title", "body", opener=raising_opener, runner=runner)

        assert len(alerts) == 1  # only the ntfy failure
        assert len(runner_calls) == 1  # toast still ran


class TestUnknownChannel:
    def test_unknown_channel_is_an_alert_not_a_crash(self):
        cfg = NotifyConfig(channel="carrier-pigeon")
        alerts = send(cfg, "title", "body")
        assert len(alerts) == 1
        assert "carrier-pigeon" in alerts[0]
