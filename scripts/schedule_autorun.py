#!/usr/bin/env python3
"""Register, remove, or check the recurring scheduled run of `autorun.py`.

`autorun.py` is one-shot by design (see its own module docstring) -- it
needs something else to actually call it on a schedule. On Windows that's
Task Scheduler; this script is a thin, stdlib-only wrapper around
`schtasks.exe` so registering (or tearing down) that polling task is one
command instead of a hand-typed `schtasks` invocation nobody remembers the
flags for.

    python scripts/schedule_autorun.py register              # every 15 min, this repo, this venv
    python scripts/schedule_autorun.py register --every 10
    python scripts/schedule_autorun.py register -- --waiver-weekday wed --waiver-hour 21
    python scripts/schedule_autorun.py status
    python scripts/schedule_autorun.py remove

Anything after a literal `--` on `register` is forwarded verbatim to every
scheduled invocation of `autorun.py` (e.g. `--waiver-weekday`/
`--waiver-hour`/`--stream`/`--no-waivers`).

Not on Windows: `register`/`status`/`remove` never touch `schtasks` (which
doesn't exist there) -- they print the equivalent `cron` line/guidance
instead and exit 0. `--dry-run` does the same on any platform: prints
exactly what would run, executes nothing.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTORUN_SCRIPT = REPO_ROOT / "scripts" / "autorun.py"
DEFAULT_TASK_NAME = "ffbot-autorun"
DEFAULT_EVERY_MINUTES = 15

# schtasks silently truncates a `/TR` longer than this -- documented Windows
# limit, not a guess. Better to fail loudly here than register a task that
# runs a mangled command every 15 minutes forever.
_TR_MAX_CHARS = 261

Runner = Callable[[list[str]], "subprocess.CompletedProcess"]


def _default_runner(cmd: list[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(cmd, capture_output=True, text=True)


def is_windows() -> bool:
    return platform.system() == "Windows"


def resolve_python(override: str | None) -> Path:
    """The interpreter the scheduled task should invoke.

    `--python` wins outright. Otherwise prefer a `pythonw.exe` sibling of
    the CURRENT interpreter (i.e. this venv's own windowless twin) when one
    exists -- a task firing every 15 minutes forever should never flash a
    console window -- and fall back to `sys.executable` (works everywhere,
    including non-Windows and venvs without a pythonw build).
    """
    if override:
        return Path(override)
    current = Path(sys.executable)
    windowless = current.with_name("pythonw.exe")
    if windowless.exists():
        return windowless
    return current


def build_register_command(
    task_name: str, every_minutes: int, python_path: Path, extra_args: Sequence[str] = (),
) -> list[str]:
    tr = build_autorun_command_line(python_path, extra_args)
    if len(tr) > _TR_MAX_CHARS:
        raise ValueError(
            f"the scheduled command is {len(tr)} chars, over schtasks' /TR limit of "
            f"{_TR_MAX_CHARS} -- schtasks would silently truncate it. Shorten the repo "
            "path, or pass fewer -- extra args."
        )
    return [
        "schtasks", "/Create", "/F",
        "/TN", task_name,
        "/SC", "MINUTE", "/MO", str(every_minutes),
        "/TR", tr,
    ]


def build_autorun_command_line(python_path: Path, extra_args: Sequence[str] = ()) -> str:
    """The exact command Task Scheduler runs on each tick, as a single
    string (`/TR` takes one quoted command, not an argv list). `--chdir`
    always points at this repo root -- see autorun.py's own docstring for
    why every relative path it resolves needs a real working directory,
    which a scheduled task has no other notion of."""
    parts = [f'"{python_path}"', f'"{AUTORUN_SCRIPT}"', "--chdir", f'"{REPO_ROOT}"']
    parts.extend(extra_args)
    return " ".join(parts)


def build_remove_command(task_name: str) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def build_status_command(task_name: str) -> list[str]:
    return ["schtasks", "/Query", "/TN", task_name, "/V", "/FO", "LIST"]


def cron_line(every_minutes: int, python_path: Path, extra_args: Sequence[str] = ()) -> str:
    extra = f" {' '.join(extra_args)}" if extra_args else ""
    return f'*/{every_minutes} * * * * cd {REPO_ROOT} && "{python_path}" scripts/autorun.py{extra}'


def cmd_register(args: argparse.Namespace, runner: Runner) -> int:
    python_path = resolve_python(args.python)
    extra = args.extra_args

    if not is_windows():
        print("schtasks is Windows-only. Add this line with `crontab -e` instead:\n")
        print(f"  {cron_line(args.every, python_path, extra)}\n")
        return 0

    try:
        cmd = build_register_command(args.task_name, args.every, python_path, extra)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(subprocess.list2cmdline(cmd))
        print(f"\nmacOS/Linux equivalent (crontab -e):\n  {cron_line(args.every, python_path, extra)}")
        return 0

    result = runner(cmd)
    if result.returncode != 0:
        print(f"schtasks failed ({result.returncode}): {result.stderr.strip()}", file=sys.stderr)
        return 1

    print(f"Registered {args.task_name!r}: autorun.py checks in every {args.every} minute(s).")
    print("\nNext:")
    print(f"  - test it now:      python scripts/autorun.py --chdir \"{REPO_ROOT}\" --dry-run")
    print("  - phone pushes:     set notify.channel/ntfy_topic in config.local.yml")
    print("  - fired reports:    reports/  (also on the GUI's /reports page)")
    return 0


def cmd_remove(args: argparse.Namespace, runner: Runner) -> int:
    if not is_windows():
        print("Not on Windows -- remove your ffbot-autorun line with `crontab -e` instead.")
        return 0

    cmd = build_remove_command(args.task_name)
    if args.dry_run:
        print(subprocess.list2cmdline(cmd))
        return 0

    result = runner(cmd)
    if result.returncode != 0:
        print(f"{args.task_name!r} is not registered (or could not be removed): {result.stderr.strip()}")
        return 0
    print(f"Removed {args.task_name!r}.")
    return 0


def cmd_status(args: argparse.Namespace, runner: Runner) -> int:
    if not is_windows():
        print("Not on Windows -- check your crontab with `crontab -l` instead.")
        return 0

    cmd = build_status_command(args.task_name)
    if args.dry_run:
        print(subprocess.list2cmdline(cmd))
        return 0

    result = runner(cmd)
    if result.returncode != 0:
        print(f"{args.task_name!r} is not registered.")
        return 0
    print(result.stdout.strip())
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    common_task_name_help = f"the scheduled task's name (default: {DEFAULT_TASK_NAME})"

    r = sub.add_parser("register", help="create/update the recurring scheduled task")
    r.add_argument("--task-name", default=DEFAULT_TASK_NAME, help=common_task_name_help)
    r.add_argument("--every", type=int, default=DEFAULT_EVERY_MINUTES, metavar="MINUTES", help=f"polling interval (default: {DEFAULT_EVERY_MINUTES})")
    r.add_argument("--python", default=None, help="interpreter to run autorun.py with (default: this venv's pythonw.exe, or its python)")
    r.add_argument("--dry-run", action="store_true", help="print the exact command instead of running it")
    r.add_argument("extra_args", nargs=argparse.REMAINDER, help="forwarded verbatim to every scheduled autorun.py invocation, after a literal --")
    r.set_defaults(func=cmd_register)

    rm = sub.add_parser("remove", help="delete the scheduled task")
    rm.add_argument("--task-name", default=DEFAULT_TASK_NAME, help=common_task_name_help)
    rm.add_argument("--dry-run", action="store_true", help="print the exact command instead of running it")
    rm.set_defaults(func=cmd_remove)

    st = sub.add_parser("status", help="show whether the task is registered, and its last run")
    st.add_argument("--task-name", default=DEFAULT_TASK_NAME, help=common_task_name_help)
    st.add_argument("--dry-run", action="store_true", help="print the exact command instead of running it")
    st.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    if getattr(args, "extra_args", None):
        # argparse.REMAINDER keeps a leading "--" if the caller typed one;
        # strip it so build_autorun_command_line doesn't pass it through
        # to autorun.py, which has no use for it either.
        if args.extra_args[0] == "--":
            args.extra_args = args.extra_args[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args, _default_runner)


if __name__ == "__main__":
    raise SystemExit(main())
