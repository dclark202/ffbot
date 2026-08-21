#!/usr/bin/env python3
"""Grade a returned training-pack responses file against the pack that
produced it, and print where a human reviewer and the engine systematically
disagree.

    python scripts/training_report.py --pack training/packs/dad-01.json \
        --responses training/responses/dad-01-dad.json

Every situation in a pack is a frozen recommendation table
(`ffbot/training.py`'s `build_scenario`) exactly as the draft room would
have shown it. This merges a reviewer's answers back onto that table
(`training.merge_responses`, which reuses `draft_report.taken_block` --
the same grading a real draft pick gets) and summarizes: overall
agreement, where in the draft the disagreement concentrates, whether it
clusters on toss-ups (noise) or on picks the engine was confident about
(signal), how the reviewer's own conviction lines up against the engine's
confidence, whether disagreement tracks a badly-built roster rather than a
bad recommendation, and which positions the reviewer favors earlier or
later than the engine does.

This is a hypothesis-generation report, not a verdict. A pattern here says
"go look at this with `scripts/backtest_draft.py`" -- it is not itself
evidence a weight should move. See docs/dev/TRAINING.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ffbot import training  # noqa: E402

FEEDBACK_DIR = Path("training/feedback")

_CONFIDENCE_BANDS = (
    (0.0, 1.6, "standout (<=1.6 live options)"),
    (1.6, 4.0, "a few real options (<=4)"),
    (4.0, float("inf"), "toss-up (>4)"),
)

# Column headers for the conviction cross-tab -- the full band labels above
# are written for a one-per-line list and are far too wide for a matrix.
_BAND_SHORT = {
    "standout (<=1.6 live options)": "standout",
    "a few real options (<=4)": "a few",
    "toss-up (>4)": "toss-up",
    "unknown": "unknown",
}

# Ordered strongest-first so the row that matters (strong conviction) is the
# first one read, and its toss-up column is the number this report exists for.
_CONVICTION_ORDER = (("strong", "strong"), ("lean", "lean"), ("toss", "toss-up"))
_HEALTH_ORDER = (("good", "good shape"), ("ok", "so-so"), ("bad", "poorly built"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--pack", required=True, help="training pack JSON (from scripts/make_training_pack.py)")
    p.add_argument("--responses", action="append", required=True, help="a returned responses JSON (repeatable, for multiple reviewers)")
    p.add_argument("--feedback-dir", default=str(FEEDBACK_DIR), help="where graded JSONL is written (default: %(default)s)")
    p.add_argument("--top", type=int, default=10, help="how many biggest disagreements to print (default: %(default)s)")
    return p.parse_args(argv)


def _read_responses(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _answered(record: dict) -> bool:
    return bool(record.get("choices")) or record.get("verdict") is not None


def _top_grade(record: dict) -> dict | None:
    graded = record.get("graded") or []
    return graded[0] if graded else None


def _confidence_band(record: dict) -> str:
    eff = (record.get("confidence") or {}).get("effective_options")
    if eff is None:
        return "unknown"
    for lo, hi, label in _CONFIDENCE_BANDS:
        if lo < eff <= hi or (lo == 0.0 and eff <= hi):
            return label
    return _CONFIDENCE_BANDS[-1][2]  # pragma: no cover -- bands are exhaustive above 0


def _disagreed(record: dict) -> bool:
    return record.get("verdict") in ("disagree", "none")


def print_conviction(answered: list[dict]) -> None:
    """Reviewer conviction against the engine's own confidence.

    The whole reason `conviction` is collected: a reviewer disagreeing on a
    twenty-way toss-up is noise, and a reviewer disagreeing where the engine
    was confident is a finding -- but so is the reverse, a reviewer who is
    CERTAIN on a table the engine reported as flat. That last cell is
    invisible without this field (round 1 of this pack had to infer it from
    the wording of free-text notes), and it is where a scarcity/need term
    that has gone slack would show up.
    """
    rated = [r for r in answered if r.get("conviction")]
    if not rated:
        return

    print("\n--- Conviction vs. the engine's own confidence ---")
    bands = [label for _, _, label in _CONFIDENCE_BANDS]
    matrix: Counter = Counter()
    for r in rated:
        matrix[(r["conviction"], _confidence_band(r))] += 1

    print("  " + "reviewer \\ engine".ljust(20) + "".join(_BAND_SHORT[b].rjust(10) for b in bands))
    for key, label in _CONVICTION_ORDER:
        if not any(matrix.get((key, b), 0) for b in bands):
            continue
        row = "".join(str(matrix.get((key, b), 0)).rjust(10) for b in bands)
        print("  " + label.ljust(20) + row)

    flat = matrix.get(("strong", "toss-up (>4)"), 0)
    if flat:
        print(f"  --> {flat} situation(s) where the reviewer was CERTAIN and the engine was flat.")
        print("      That pairing is the one worth tuning on: the engine expressed no")
        print("      preference exactly where an experienced drafter had a clear one.")

    unrated = len(answered) - len(rated)
    if unrated:
        print(f"  ({unrated} answered situation(s) carried no conviction rating)")


def print_roster_health(answered: list[dict]) -> None:
    """How the reviewer rated the partial roster each situation was built on.

    Separates a complaint about the SITUATION from a complaint about the
    RECOMMENDATION. A synthetic roster is bot-drafted, so "only two RB by
    round 7" may be an objection to picks 1-6 rather than to the pick on the
    clock -- and the two demand completely different responses. The
    disagreement rate split by this rating is the payoff: if disagreement
    tracks a badly-built roster, the recommendation may be fine and the bot
    is what's wrong.
    """
    rated = [r for r in answered if r.get("roster_health")]
    if not rated:
        return

    print("\n--- Roster health (rating the bot-built roster, not the pick) ---")
    counts = Counter(r["roster_health"] for r in rated)
    print("  " + "   ".join(f"{label}: {counts.get(key, 0)}" for key, label in _HEALTH_ORDER))
    for key, label in _HEALTH_ORDER:
        rows = [r for r in rated if r["roster_health"] == key]
        if not rows:
            continue
        dis = sum(1 for r in rows if _disagreed(r))
        print(f"  disagreement rate when roster rated {label:<13}: {dis}/{len(rows)} ({100 * dis / len(rows):.0f}%)")
    print("  (if disagreement tracks a poorly-built roster, the recommendation may be")
    print("   fine and the roster it was given is the thing to look at)")

    # The ratings say THAT a roster was wrong; only the notes say how, which
    # is the part a bot-spice change can actually be steered by. Ordered
    # worst-first so the complaints lead.
    order = {key: i for i, (key, _) in enumerate(_HEALTH_ORDER)}
    noted = sorted(
        (r for r in rated if (r.get("roster_note") or "").strip()),
        key=lambda r: (-order.get(r["roster_health"], 0), r["pick"]),
    )
    if noted:
        labels = dict(_HEALTH_ORDER)
        print("\n  What they said about the rosters:")
        for r in noted:
            print(f"    pick {r['pick']:>3} ({r['round_bucket']}, {labels.get(r['roster_health'], '?')}): {r['roster_note'].strip()}")


def _slug(reviewer: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in reviewer.strip())
    return safe.strip("-") or "reviewer"


def _write_feedback_jsonl(records: list[dict], pack_id: str, reviewer: str, out_dir: Path) -> Path | None:
    path = out_dir / f"{pack_id}-{_slug(reviewer)}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
    except OSError:
        return None
    return path


def _fmt(n, digits=1) -> str:
    return "-" if n is None else f"{n:.{digits}f}"


def print_summary(pack: dict, all_records: list[dict], top_n: int) -> None:
    total = len(pack["scenarios"])
    answered = [r for r in all_records if _answered(r)]

    print(f"\n=== {pack.get('label') or pack['pack_id']} ({pack['pack_id']}) ===")
    print(f"{len(answered)} answered of {total} scenarios, across {len({r.get('reviewer') for r in all_records})} reviewer(s)")

    if not answered:
        print("nothing answered yet -- nothing to summarize")
        return

    verdicted = [r for r in answered if r.get("verdict")]
    agree = sum(1 for r in verdicted if r["verdict"] == "agree")
    with_choice = [r for r in answered if r.get("choices")]
    top_matches = sum(1 for r in with_choice if (_top_grade(r) or {}).get("was_top_recommendation"))
    ranks = [g["rank_in_table"] for r in with_choice if (g := _top_grade(r)) and g.get("rank_in_table") is not None]
    gaps = [g["value_gap_to_top"] for r in with_choice if (g := _top_grade(r)) and g.get("value_gap_to_top") is not None]
    off_table = sum(1 for r in with_choice if (g := _top_grade(r)) and g.get("rank_in_table") is None)

    print("\n--- Overall ---")
    if verdicted:
        print(f"agree rate: {agree}/{len(verdicted)} ({100 * agree / len(verdicted):.0f}%)")
    if with_choice:
        print(f"reviewer's #1 == engine's #1: {top_matches}/{len(with_choice)} ({100 * top_matches / len(with_choice):.0f}%)")
        if ranks:
            print(f"mean rank (in engine's table) of reviewer's #1: {sum(ranks) / len(ranks):.1f}")
        if gaps:
            print(f"mean value gap to engine's #1: {sum(gaps) / len(gaps):.1f} pts")
        if off_table:
            print(f"reviewer's #1 wasn't in the engine's table at all: {off_table}/{len(with_choice)}")

    print("\n--- By round ---")
    by_round: dict[str, list[dict]] = defaultdict(list)
    for r in with_choice:
        by_round[r["round_bucket"]].append(r)
    for bucket in ("R1-2", "R3-5", "R6-9", "R10+"):
        rows = by_round.get(bucket)
        if not rows:
            continue
        matches = sum(1 for r in rows if (_top_grade(r) or {}).get("was_top_recommendation"))
        rg = [g["value_gap_to_top"] for r in rows if (g := _top_grade(r)) and g.get("value_gap_to_top") is not None]
        gap_s = f", mean gap {sum(rg) / len(rg):.1f}" if rg else ""
        print(f"  {bucket}: {matches}/{len(rows)} matched engine's #1{gap_s}")

    print("\n--- By confidence band ---")
    by_band: dict[str, list[dict]] = defaultdict(list)
    for r in answered:
        by_band[_confidence_band(r)].append(r)
    for _, _, label in _CONFIDENCE_BANDS:
        rows = by_band.get(label)
        if not rows:
            continue
        disagree = sum(1 for r in rows if r.get("verdict") in ("disagree", "none"))
        print(f"  {label}: {disagree}/{len(rows)} disagreed ({100 * disagree / len(rows):.0f}%)")
    print("  (disagreement on toss-ups is expected noise; disagreement where the")
    print("   engine was confident is the signal worth a second look)")

    print_conviction(answered)
    print_roster_health(answered)

    print("\n--- Positional bias: engine's top rec vs. reviewer's #1 ---")
    matrix: Counter = Counter()
    for r in with_choice:
        g = _top_grade(r)
        engine_pos = r.get("top_rec_position") or "?"
        my_pos = (g or {}).get("position") or "?"
        matrix[(engine_pos, my_pos)] += 1
    if matrix:
        positions = sorted({p for pair in matrix for p in pair})
        header = "engine \\ reviewer".ljust(18) + "".join(p.rjust(6) for p in positions)
        print(f"  {header}")
        for ep in positions:
            row = "".join(str(matrix.get((ep, mp), 0)).rjust(6) for mp in positions)
            print(f"  {ep.ljust(18)}{row}")

    print(f"\n--- Biggest disagreements (top {top_n} by value gap) ---")
    scored = []
    for r in with_choice:
        g = _top_grade(r)
        if g and g.get("value_gap_to_top") is not None:
            scored.append((g["value_gap_to_top"], r, g))
    scored.sort(key=lambda t: -t[0])
    for gap, r, g in scored[:top_n]:
        note = f" -- {r['note']}" if r.get("note") else ""
        conviction = f" [{r['conviction']}]" if r.get("conviction") else ""
        print(
            f"  pick {r['pick']:>3} ({r['round_bucket']}): took {g.get('name') or g.get('key')} "
            f"(rank {g.get('rank_in_table')}, -{gap:.0f} pts vs. top){conviction}{note}"
        )

    print(
        "\nThese are hypotheses, not verdicts -- any pattern here goes through "
        "scripts/backtest_draft.py before a weight moves. See docs/dev/TRAINING.md."
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        pack = training.read_pack(args.pack)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read pack {args.pack} ({exc})", file=sys.stderr)
        return 1

    out_dir = Path(args.feedback_dir)
    all_records: list[dict] = []
    for responses_path in args.responses:
        try:
            responses = _read_responses(responses_path)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: could not read responses {responses_path} ({exc})", file=sys.stderr)
            return 1
        if responses.get("pack_id") and responses["pack_id"] != pack["pack_id"]:
            print(
                f"warning: {responses_path} was answered against pack "
                f"{responses['pack_id']!r}, not {pack['pack_id']!r} -- grading anyway",
                file=sys.stderr,
            )
        records, warnings_ = training.merge_responses(pack, responses)
        for w in warnings_:
            print(f"  {responses_path}: {w}", file=sys.stderr)
        reviewer = responses.get("reviewer") or Path(responses_path).stem
        for r in records:
            r["reviewer"] = reviewer
        written = _write_feedback_jsonl(records, pack["pack_id"], reviewer, out_dir)
        if written is not None:
            print(f"  {responses_path}: {len(records)} graded answers -> {written}")
        all_records.extend(records)

    print_summary(pack, all_records, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
