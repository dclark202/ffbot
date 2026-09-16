"""Unattended weekly research: a headless Claude Code run that researches the
week and writes `weekly/week-NN.yml`, the file `ffbot.week` already reads.

`ffbot.week`'s design always left this slot open -- researched notes are "the
job of whatever populates a weekly/week-NN.yml file ... this module never
cares which one did it" -- but until now the only thing that ever filled it
was a human running `/gameday`. `scripts/autorun.py` calls `run_research`
right before a check, so the report reads fresh research instead of none.

This is the one seam in the repo that lets a model with web access write a
file that moves numbers, with nobody reviewing it before a check runs on it.
So it holds the repo's live-seam contract -- an injectable `runner`,
`run_research` never raises, every failure is a surfaced alert -- plus
guardrails enforced in CODE rather than trusted to the prompt:

- **Tool allow-list.** `--permission-mode dontAsk` denies every tool not
  listed, and the list is web search/fetch, Read, and Edit scoped to the one
  week file. `Edit(path)` is the one file rule the CLI matches, and it
  covers Write too -- a `Write(path)` rule alongside it made the CLI refuse
  to start (exit 129) on 2026-09-15, which cost that week's pre-waiver
  research. No shell, no MCP servers, nothing else on disk.
- **Official sources only for `status`.** A researched status overrides
  Sleeper's live one, so a hallucinated or planted "O" would silently bench a
  starter. After every run, a status whose `source` is not an http(s) URL on
  `ResearchConfig.official_source_domains` is downgraded to a note.
- **Validate or roll back.** A week file that no longer loads is restored to
  its exact pre-run bytes; `ffbot.report` would otherwise lose the check.
- **Slot runs never delete.** A pre-kickoff pass covers one kickoff time's
  games, but the file holds the whole week -- an entry a slot run dropped is
  put back, so Friday's research on Sunday's other games survives.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import yaml

from .config import ResearchConfig

Runner = Callable[..., "subprocess.CompletedProcess"]

COMMAND_FILE = Path(".claude") / "commands" / "research-week.md"

_SKELETON = 'week: {week}\ngenerated: ""\nsource_notes: ""\nplayers: {{}}\ngames: {{}}\n'

_FALLBACK = "check used live data only"


@dataclass(frozen=True)
class ResearchContext:
    """Everything the research run is told. `scripts/autorun.py` builds it
    from a live report, so the model never has to guess the week, the roster
    or the kickoff -- the things `/gameday` stops to ask a human about."""

    season: int
    week: int
    mode: str  # "full" (the whole week) | "slot" (one kickoff time's games)
    kickoff_et: str = ""
    slot_teams: tuple[str, ...] = ()
    roster: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


@dataclass
class ResearchResult:
    """`ok` means the week file was updated and passed every check. `alerts`
    say why not, in words fit for a push notification. `overrides` are the
    statuses that survived the official-source rule -- the ones that will
    override Sleeper -- and `downgraded` the ones that did not."""

    ok: bool = False
    alerts: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    downgraded: list[str] = field(default_factory=list)
    transcript: str = ""


def _default_runner(args: list[str], **kwargs) -> "subprocess.CompletedProcess":
    return subprocess.run(args, **kwargs)


def resolve_claude(cfg: ResearchConfig) -> Optional[str]:
    """The Claude Code CLI to run: `research.claude_path` when set, else
    `claude` on PATH, else the installer's default `~/.local/bin` -- which a
    scheduled task's PATH often lacks even when a terminal's has it."""
    if cfg.claude_path:
        return cfg.claude_path if Path(cfg.claude_path).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        candidate = Path.home() / ".local" / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


def official_host(url: object, domains: Iterable[str]) -> Optional[str]:
    """The host of `url` when it is an http(s) URL on one of `domains` or a
    subdomain of one, else None. `nfl.com.example.io` is not `nfl.com`."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    for domain in domains:
        d = str(domain).strip().lower().removeprefix("www.")
        if d and (host == d or host.endswith("." + d)):
            return host
    return None


def allowed_tools(week_rel: str) -> list[str]:
    """Every tool the research run may use. Under `--permission-mode dontAsk`
    anything not on this list is denied outright, so this IS the boundary."""
    return ["WebSearch", "WebFetch", "Read", f"Edit({week_rel})"]


def build_argv(claude: str, week_rel: str) -> list[str]:
    # Pinned rather than left to whatever the CLI's own default happens to
    # be -- a research pass is fetch-and-summarize, not a task that needs a
    # bigger model, and pinning it here means it can't silently change if
    # the CLI's default model does.
    return [
        claude, "-p",
        "--model", "claude-sonnet-5",
        "--effort", "medium",
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed_tools(week_rel),
        "--disallowedTools", "Bash",
        "--strict-mcp-config",
        "--output-format", "text",
    ]


def build_prompt(ctx: ResearchContext, week_rel: str, command_text: str) -> str:
    """The research command's body with its frontmatter stripped and
    `$ARGUMENTS` replaced by the context block."""
    body = command_text
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + len("\n---"):]

    lines = [f"mode: {ctx.mode}", f"season: {ctx.season}", f"week: {ctx.week}", f"file: {week_rel}"]
    if ctx.kickoff_et:
        lines.append(f"slot kickoff (ET): {ctx.kickoff_et}")
    if ctx.slot_teams:
        lines.append("slot teams: " + ", ".join(ctx.slot_teams))
    lines.append("roster:")
    if ctx.roster:
        lines.extend(f"  - {r}" for r in ctx.roster)
    else:
        lines.append("  (none supplied)")
    lines.append("candidates:")
    if ctx.candidates:
        lines.extend(f"  - {c}" for c in ctx.candidates)
    else:
        lines.append("  (none)")
    return body.replace("$ARGUMENTS", "\n".join(lines)).strip() + "\n"


def run_research(
    ctx: ResearchContext,
    cfg: ResearchConfig,
    week_path: str | Path,
    *,
    repo_root: str | Path = ".",
    runner: Optional[Runner] = None,
    command_text: Optional[str] = None,
) -> ResearchResult:
    """Run one research pass and leave `week_path` either updated and valid,
    or exactly as it was. Never raises."""
    result = ResearchResult()
    week_path = Path(week_path)
    repo_root = Path(repo_root)
    before: Optional[bytes] = None
    snapshotted = False
    try:
        claude = resolve_claude(cfg)
        if claude is None:
            result.alerts.append(f"research skipped: Claude Code CLI not found (set research.claude_path) — {_FALLBACK}")
            return result
        if command_text is None:
            command_text = (repo_root / COMMAND_FILE).read_text(encoding="utf-8")

        before = week_path.read_bytes() if week_path.exists() else None
        snapshotted = True
        if before is None:
            week_path.parent.mkdir(parents=True, exist_ok=True)
            week_path.write_text(_SKELETON.format(week=ctx.week), encoding="utf-8")
        seeded = week_path.read_bytes()

        week_rel = Path(os.path.relpath(week_path, repo_root)).as_posix()
        minutes = cfg.full_timeout_minutes if ctx.mode == "full" else cfg.slot_timeout_minutes
        run = runner if runner is not None else _default_runner
        try:
            proc = run(
                build_argv(claude, week_rel),
                input=build_prompt(ctx, week_rel, command_text).encode("utf-8"),
                capture_output=True,
                timeout=minutes * 60,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            _restore(week_path, before)
            result.alerts.append(f"research timed out after {minutes:g} min — rolled back, {_FALLBACK}")
            return result

        out = _text(getattr(proc, "stdout", None))
        err = _text(getattr(proc, "stderr", None))
        result.transcript = out.strip()
        if "not logged in" in (out + err).lower():
            _restore(week_path, before)
            result.alerts.append(
                "research didn't run: the Claude Code CLI isn't logged in "
                f"(run `claude` in a terminal and /login) — {_FALLBACK}"
            )
            return result
        returncode = getattr(proc, "returncode", 0)
        if returncode != 0:
            _restore(week_path, before)
            result.alerts.append(f"research failed (exit {returncode}: {_tail(err or out)}) — rolled back, {_FALLBACK}")
            return result

        after = week_path.read_bytes() if week_path.exists() else b""
        if after == seeded:
            _restore(week_path, before)
            result.alerts.append(f"research finished without updating the week file — {_FALLBACK}")
            return result

        from . import week as weekmod

        try:
            raw = yaml.safe_load(after.decode("utf-8")) or {}
            weekmod.load_weekly_intel(week_path)
        except (weekmod.WeeklyIntelError, yaml.YAMLError, UnicodeDecodeError, ValueError, TypeError) as exc:
            _restore(week_path, before)
            result.alerts.append(f"research wrote an unreadable week file ({_tail(str(exc))}) — rolled back, {_FALLBACK}")
            return result

        changed = False
        if ctx.mode == "slot" and before is not None:
            changed |= _restore_deleted(raw, before)
        changed |= _enforce_official_sources(raw, cfg.official_source_domains, result)
        if changed:
            week_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
            weekmod.load_weekly_intel(week_path)
        result.ok = True
        return result
    except Exception as exc:  # noqa: BLE001 -- the seam contract: a research failure never takes down a check
        if snapshotted:
            _restore(week_path, before)
        result.ok = False
        result.alerts.append(f"research failed unexpectedly ({_tail(str(exc))}) — rolled back, {_FALLBACK}")
        return result


def _restore(path: Path, before: Optional[bytes]) -> None:
    """Put the week file back exactly as it was -- or remove it when there was
    none, since a missing file is `ffbot.week`'s inert "no research" state."""
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(before)
    except OSError:
        pass


def _restore_deleted(raw: dict, before: bytes) -> bool:
    """Re-add any player or game entry a slot run dropped. A slot pass covers
    one kickoff time, but the file holds the whole week."""
    try:
        prior = yaml.safe_load(before.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError):
        return False
    if not isinstance(prior, dict):
        return False
    changed = False
    for section in ("players", "games"):
        old = prior.get(section)
        if not isinstance(old, dict):
            continue
        current = raw.get(section)
        if not isinstance(current, dict):
            current = {}
            raw[section] = current
        for key, value in old.items():
            if key not in current:
                current[key] = value
                changed = True
    return changed


def _enforce_official_sources(raw: dict, domains: Iterable[str], result: ResearchResult) -> bool:
    """Downgrade every `status` not backed by an official source to a note,
    recording both the survivors and the downgrades on `result`."""
    players = raw.get("players")
    if not isinstance(players, dict):
        return False
    changed = False
    for name, entry in players.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").strip().upper()
        if not status:
            continue
        source = entry.get("source")
        host = official_host(source, domains)
        if host is not None:
            result.overrides.append(f"{name}: {status} ({host})")
            continue
        shown = str(source).strip() if source else "no source"
        note = " ".join(str(entry.get("note") or "").split())
        entry["note"] = f"unverified status {status} ({shown})" + (f"; {note}" if note else "")
        entry.pop("status", None)
        result.downgraded.append(f"{name}: {status} ({shown})")
        changed = True
    return changed


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _tail(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else "…" + flat[-limit:]
