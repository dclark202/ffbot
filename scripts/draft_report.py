#!/usr/bin/env python3
"""Rebuild a draft's JSON report from its pick log, by replay.

    python scripts/draft_report.py --log draft/mock_live_log.jsonl
    python scripts/draft_report.py --log draft_log.jsonl --slot 4 --out draft/reports/mine.json
    python scripts/draft_report.py --all            # every *.jsonl log in the repo + draft/

A live draft already writes one of these as it goes (see
`ffbot/draft_report.py`); this exists for the logs that predate that, and
for re-deriving a report under a DIFFERENT config to see how a tuning change
would have altered the same draft.

That second use is the point: the recommendation table is recomputed from
the board the log's picks imply, so running this with an edited `config.yml`
answers "what would the engine have said at pick 45 if I changed X" against
a real draft rather than a synthetic one.

Because the table is recomputed rather than remembered, a replayed report is
stamped `"source": "replay"` and carries the CURRENT board provenance. If
`draft.board_points_source` is `sleeper`, the live season projections behind
it move during preseason, so a replay run weeks later is not guaranteed to
reproduce the numbers a live report captured at the time -- that is exactly
why the live path stores its own.

Ownership: a log records `mine` per pick as it was resolved live (from
Sleeper's `roster_id` when configured), so replay preserves it. When a log
carries no ownership at all, `--slot` supplies the snake-order fallback and
the report says so via `ownership_source`.
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot.board import load_board_from_config  # noqa: E402
from ffbot.config import Config  # noqa: E402
from ffbot.draft import DraftState, recommend  # noqa: E402
from ffbot.draft_report import build_report, capture_pick, report_path, write_report  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", action="append", default=None, help="draft log path (repeatable)")
    p.add_argument("--all", action="store_true", help="every draft_log*.jsonl and draft/*.jsonl found")
    p.add_argument("--config", default="config.yml", help="(default: %(default)s)")
    p.add_argument("--slot", type=int, default=None, help="your draft slot, when the log has no ownership")
    p.add_argument("--limit", type=int, default=None, help="rows of the rec table to store per pick (default: draft.gui_recommend_count)")
    p.add_argument(
        "--sessions", choices=["last", "all"], default="last",
        help="a log can hold several drafts appended together (an abandoned mock, a restart); "
             "'last' reports the most recent (default), 'all' reports every one",
    )
    p.add_argument("--out", default=None, help="output path (single --log only; default: draft/reports/<log stem>.json)")
    p.add_argument("--out-dir", default="draft/reports", help="(default: %(default)s)")
    return p.parse_args(argv)


def _discover_logs() -> list[Path]:
    seen: dict[Path, None] = {}
    for pattern in ("draft_log*.jsonl", "draft/*.jsonl"):
        for path in sorted(Path(".").glob(pattern)):
            if path.is_file() and path.stat().st_size > 0:
                seen.setdefault(path.resolve(), None)
    return [Path(p) for p in seen]


def _read_log(log_path: Path) -> list[dict]:
    """Split one log file into its separate draft SESSIONS.

    A log is append-only across drafts: `scripts/draft.py` archives on an
    explicit `reset`, but a mock that is simply abandoned and restarted
    leaves its picks in the same file. Real logs here hold five sessions.
    Replaying that as one continuous draft is not a small error — pick
    numbers run past the final round, the same player appears twice, and
    ownership scrambles, producing a report that looks plausible and
    describes nothing that happened.

    Two boundary signals, both needed. A `draft_id` line marks a new live
    session, and a `sync` entry whose pick number does not advance marks a
    restart (including offline sessions, which never write a draft_id).

    Returns `[{draft_id, picks: [{key, mine}, ...]}, ...]` in file order.
    A `line` entry (an interactively typed name) becomes an unknown pick
    rather than being re-resolved through the name search: a search string
    can legitimately resolve to a different player against a board rebuilt
    later, and silently attributing the wrong player to a pick would
    corrupt the very record this exists to be.
    """
    segments: list[dict] = []
    current: dict = {"draft_id": None, "picks": []}
    last_number: int | None = None

    def _rotate(draft_id: str | None = None) -> None:
        nonlocal current, last_number
        if current["picks"]:
            segments.append(current)
        current = {"draft_id": draft_id, "picks": []}
        last_number = None

    with open(log_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if "draft_id" in obj:
                if current["picks"]:
                    _rotate(obj["draft_id"])
                else:
                    current["draft_id"] = obj["draft_id"]
            elif "sync" in obj:
                number = obj["sync"].get("number")
                if number is not None and last_number is not None and number <= last_number:
                    _rotate(current["draft_id"])
                if number is not None:
                    last_number = number
                current["picks"].append(
                    {"key": obj["sync"].get("key"), "mine": obj["sync"].get("mine")}
                )
            elif "pick" in obj:
                current["picks"].append(
                    {"key": obj["pick"].get("key"), "mine": obj["pick"].get("mine")}
                )
            elif "line" in obj:
                current["picks"].append({"key": None, "mine": None})

    if current["picks"]:
        segments.append(current)
    return segments


def build_from_segment(segment: dict, cfg: Config, board, slot: int | None, limit: int) -> dict:
    picks, draft_id = segment["picks"], segment["draft_id"]
    has_ownership = any(p.get("mine") is not None for p in picks)
    state = DraftState(
        board=board,
        num_teams=cfg.draft.num_teams,
        my_slot=slot if slot is not None else cfg.draft.my_slot,
        rounds=cfg.draft.rounds,
        roster_positions=cfg.roster_positions,
        my_picks_override=list(cfg.draft.my_picks),
        order=cfg.draft.order,
    )

    captured: list[dict] = []
    for entry in picks:
        mine = entry.get("mine")
        is_mine = mine if mine is not None else (state.current_pick() in state.my_picks())
        if is_mine and entry.get("key"):
            recs = recommend(state, cfg, limit=limit)
            captured.append(capture_pick(state, cfg, recs, entry["key"]))
        try:
            state.record(entry.get("key"), mine=mine, source="api")
        except ValueError:
            continue

    return build_report(
        state, cfg, captured,
        draft_id=draft_id,
        ownership_source="log_recorded" if has_ownership else "snake_order_guess",
        source="replay",
        board=board,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logs = [Path(p) for p in (args.log or [])]
    if args.all:
        logs.extend(p for p in _discover_logs() if p not in logs)
    if not logs:
        print("error: pass --log <path> (repeatable) or --all", file=sys.stderr)
        return 1
    if args.out and len(logs) > 1:
        print("error: --out only applies to a single --log; use --out-dir", file=sys.stderr)
        return 1

    cfg = Config.load(args.config)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            board = load_board_from_config(cfg)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    limit = args.limit if args.limit is not None else cfg.draft.gui_recommend_count
    written = 0
    for log_path in logs:
        if not log_path.exists():
            print(f"  {log_path}: not found — skipped", file=sys.stderr)
            continue
        segments = _read_log(log_path)
        if not segments:
            print(f"  {log_path}: empty — skipped", file=sys.stderr)
            continue
        chosen = segments if args.sessions == "all" else segments[-1:]
        if len(segments) > 1:
            print(f"  {log_path.name}: {len(segments)} draft sessions in this log; reporting {len(chosen)}")

        for idx, segment in enumerate(chosen, start=1):
            report = build_from_segment(segment, cfg, board, args.slot, limit)
            if not report["picks"]:
                print(f"    session {idx}: no identifiable picks of yours — skipped", file=sys.stderr)
                continue
            if args.out:
                dest = Path(args.out)
            else:
                suffix = f"-{report['draft_id']}" if report["draft_id"] else (
                    f"-s{idx}" if len(chosen) > 1 else ""
                )
                dest = Path(args.out_dir) / f"{log_path.stem}{suffix}.json"
            write_report(report, dest)
            counts = report["final_position_counts"]
            print(f"    {len(report['picks'])} of my picks -> {dest}  roster={counts}")
            written += 1

    if not written:
        print("nothing written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
