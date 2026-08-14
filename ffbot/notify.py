"""Outbound push notifications for unattended runs (`scripts/autorun.py`).

Every other live seam in this repo is INBOUND — a fetch that might fail,
degrading to "no data this run" with a surfaced alert, never a crash (see
CLAUDE.md's design invariants). This module is the one OUTBOUND seam, held
to the identical contract: `send()` never raises. A delivery failure
returns a human-readable alert string instead, and the caller (`autorun.py`)
prints it to stderr without un-marking the trigger that produced it — a
notification that failed to send is not a reason to re-run an otherwise-
successful report.

Two channels, both real, both optional (`NotifyConfig.channel`, default
`"off"`):

- `"ntfy"` — a POST to a free, no-signup push topic (https://ntfy.sh by
  default). The topic name IS the secret (the server is public), so it
  belongs in `config.local.yml`, never in the checked-in `config.yml`.
- `"toast"` — a local Windows notification via PowerShell's
  `ToastNotificationManager`, no external service, only seen if you're at
  the machine when it fires.

`runner`/`opener` are injectable (matching every other network-touching
module here — `ffbot.sleeper.cache.fetch_cached`'s `opener`, in
particular) so `tests/test_notify.py` never touches a real subprocess or
a real network socket.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .config import NotifyConfig

Runner = Callable[..., "subprocess.CompletedProcess"]
Opener = Callable[[urllib.request.Request], bytes]


class NotifyError(RuntimeError):
    """A notification could not be delivered — internal to this module;
    `send()` catches every case of this and returns an alert string
    instead of letting it propagate."""


def _default_opener(req: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - fixed https hosts only
        return resp.read()


def _default_runner(args: list[str], **kwargs) -> "subprocess.CompletedProcess":
    return subprocess.run(args, capture_output=True, timeout=20, **kwargs)


def _send_ntfy(cfg: NotifyConfig, title: str, body: str, opener: Opener) -> Optional[str]:
    """Returns an alert string on failure, `None` on success. A blank
    `ntfy_topic` is a configuration mistake, not a network failure — caught
    before any request is attempted, same "don't even ask" pattern every
    optional live seam in this repo uses for a not-yet-configured feature.
    """
    topic = cfg.ntfy_topic.strip()
    if not topic:
        return "ntfy: no ntfy_topic configured (set notify.ntfy_topic in config.local.yml) — notification not sent"
    server = cfg.ntfy_server.rstrip("/")
    # Title travels as a query parameter, not an HTTP header -- header
    # values must be latin-1 encodable and a player name/report title can
    # easily not be (accents, etc.); a query param has no such restriction
    # once percent-encoded, and ntfy accepts both forms identically.
    url = f"{server}/{topic}?title={urllib.parse.quote(title)}"
    req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
    try:
        opener(req)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return f"ntfy: delivery failed ({exc})"
    return None


def _send_toast(title: str, body: str, runner: Runner) -> Optional[str]:
    """A local Windows toast via PowerShell. Both strings are passed
    through PowerShell single-quoted literals (only `'` needs escaping
    there, doubled per PowerShell's own quoting rule) rather than
    interpolated into the script text some other way, so nothing in
    `title`/`body` can be read as PowerShell syntax."""

    def _ps_literal(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$texts = $xml.GetElementsByTagName('text');"
        f"$texts.Item(0).AppendChild($xml.CreateTextNode({_ps_literal(title)})) > $null;"
        f"$texts.Item(1).AppendChild($xml.CreateTextNode({_ps_literal(body)})) > $null;"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ffbot').Show($toast);"
    )
    try:
        result = runner(["powershell", "-NoProfile", "-Command", script])
    except (OSError, subprocess.SubprocessError) as exc:
        return f"toast: could not run powershell ({exc})"
    if result.returncode != 0:
        stderr = getattr(result, "stderr", b"")
        detail = stderr.decode("utf-8", errors="replace").strip() if isinstance(stderr, bytes) else str(stderr).strip()
        return f"toast: powershell exited {result.returncode}" + (f" ({detail})" if detail else "")
    return None


_CHANNELS = {"ntfy": _send_ntfy, "toast": _send_toast}


def send(
    cfg: NotifyConfig,
    title: str,
    body: str,
    *,
    runner: Optional[Runner] = None,
    opener: Optional[Opener] = None,
) -> list[str]:
    """Deliver `title`/`body` via `cfg.channel`. Returns a list of
    human-readable failure alerts — empty on full success, and ALWAYS
    empty (never raises) for `channel == "off"`, which doesn't attempt
    delivery at all. `"both"` fans out to ntfy and toast independently; a
    failure on one never blocks the other.
    """
    resolved_runner = runner if runner is not None else _default_runner
    resolved_opener = opener if opener is not None else _default_opener

    channels: list[str]
    if cfg.channel == "off":
        channels = []
    elif cfg.channel == "both":
        channels = ["ntfy", "toast"]
    elif cfg.channel in _CHANNELS:
        channels = [cfg.channel]
    else:
        return [f"notify: unknown channel {cfg.channel!r} (must be off/ntfy/toast/both) — notification not sent"]

    alerts: list[str] = []
    for channel in channels:
        if channel == "ntfy":
            alert = _send_ntfy(cfg, title, body, resolved_opener)
        else:
            alert = _send_toast(title, body, resolved_runner)
        if alert:
            alerts.append(alert)
    return alerts
