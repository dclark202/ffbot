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
import re
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
    p.add_argument("--reviewer", default=None, help="name for the graded records, overriding the responses file's own (which is often blank)")
    p.add_argument(
        "--rounds",
        default=None,
        help=(
            "restrict to one or more round buckets, e.g. \"R3-9\" or \"R3-5,R6-9\". "
            "Two packs built with different --rounds-range are not comparable "
            "without this -- see the caution printed under Overall."
        ),
    )
    p.add_argument("--append", action="store_true", help="append to the graded JSONL instead of replacing it (default: replace, so re-grading the same file twice cannot double-count)")
    p.add_argument("--top", type=int, default=10, help="how many biggest disagreements to print (default: %(default)s)")
    return p.parse_args(argv)


# `--rounds R3-9` is written the way a human says it ("rounds three to
# nine"), not as a list of `training._ROUND_BUCKETS` labels, because the
# buckets are an implementation detail of stratification. Expand the shorthand
# to the buckets it overlaps; an exact bucket label still works unchanged.
_BUCKET_RANGES: dict[str, tuple[int, int]] = {
    "R1-2": (1, 2), "R3-5": (3, 5), "R6-9": (6, 9), "R10+": (10, 999),
}


def parse_round_filter(spec: str | None) -> set[str] | None:
    """`"R3-9"` / `"R3-5,R10+"` -> the set of bucket labels it covers.

    `None` (no filter) returns `None` rather than every bucket, so callers can
    tell "no filter asked for" from "a filter that happens to match all" --
    only the former should stay silent in the report header.
    """
    if not spec:
        return None
    out: set[str] = set()
    for part in (chunk.strip() for chunk in spec.split(",")):
        if not part:
            continue
        if part in _BUCKET_RANGES:
            out.add(part)
            continue
        m = re.fullmatch(r"[Rr](\d+)-(\d+|\+)", part)
        if not m:
            raise ValueError(
                f"unrecognized round filter {part!r} -- expected e.g. R3-9, R10+, or a bucket label"
            )
        lo = int(m.group(1))
        hi = 999 if m.group(2) == "+" else int(m.group(2))
        for label, (blo, bhi) in _BUCKET_RANGES.items():
            if blo <= hi and lo <= bhi:
                out.add(label)
    if not out:
        raise ValueError(f"round filter {spec!r} matched no rounds")
    return out


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


def roster_provenance(pack: dict) -> list[str]:
    """Line(s) naming WHO drafted the rosters the reviewer was rating.

    `scripts/make_training_pack.py --my-spice` is meant to differ from the
    tool's own configured level, so a situation is not a replay of the
    engine's own choices (see docs/dev/TRAINING.md). When it does NOT differ,
    every roster complaint is a verdict on the ENGINE's roster construction
    rather than on a bot's -- a much stronger reading of the same words, and
    one nothing in this output used to say. Review round 2 was generated that
    way and it went unremarked until the responses were read by hand.

    Returns `[]` when the pack predates `generator.my_spice` or omits it --
    silence is correct there, since the question genuinely cannot be answered.
    """
    gen = pack.get("generator") or {}
    my_spice = gen.get("my_spice")
    cfg_spice = (pack.get("config") or {}).get("spice_level")
    if my_spice is None:
        return []
    if cfg_spice is not None and my_spice == cfg_spice:
        return [
            f"  NOTE: rosters were drafted at spice {my_spice}, the SAME level the engine's",
            "  own recommendations use -- so a roster complaint here is a complaint about",
            "  the ENGINE's roster construction, not about a bot's.",
        ]
    return [
        f"  rosters were drafted at spice {my_spice} against an engine configured at "
        f"{cfg_spice} --",
        "  a roster complaint is about the bot in that seat, not about the recommendation.",
    ]


def print_roster_health(answered: list[dict], pack: dict | None = None) -> None:
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

    print("\n--- Roster health (rating the roster it was given, not the pick) ---")
    for line in roster_provenance(pack or {}):
        print(line)
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


def _write_feedback_jsonl(
    records: list[dict], pack_id: str, reviewer: str, out_dir: Path, append: bool = False,
) -> Path | None:
    """Write the graded records for one reviewer, REPLACING the file by default.

    This used to be unconditionally append-only, which is wrong for the way
    the file is actually produced: grading is deterministic and re-run freely
    (a new metric, a fixed bug, a corrected reviewer name), and every re-run
    silently doubled the file. Nothing reads these yet, so nothing has been
    burned by it -- but the whole point of the JSONL is to be pooled across
    rounds later, and a pooled count that quietly depends on how many times
    somebody re-ran a report is not a measurement.

    `append=True` is still available for the one case that wants it: adding a
    second reviewer's answers to the same pack under the same name.
    """
    path = out_dir / f"{pack_id}-{_slug(reviewer)}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if append else "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
    except OSError:
        return None
    return path


def _fmt(n, digits=1) -> str:
    return "-" if n is None else f"{n:.{digits}f}"


def print_summary(
    pack: dict, all_records: list[dict], top_n: int, buckets: set[str] | None = None,
) -> None:
    """Summarize graded answers, optionally restricted to `buckets`.

    The filter exists because two packs built with different
    `--rounds-range` are NOT comparable, and comparing them anyway is the
    easiest mistake this report invites. Review rounds 1 and 2 differed by
    57% vs. 17% on agree rate purely because round 2 sampled only rounds
    3-9 -- restricted to the rounds both covered, every metric had moved
    the other way.
    """
    scenarios = pack["scenarios"]
    if buckets is not None:
        scenarios = [sc for sc in scenarios if sc["round_bucket"] in buckets]
        all_records = [r for r in all_records if r.get("round_bucket") in buckets]
    total = len(scenarios)
    answered = [r for r in all_records if _answered(r)]

    print(f"\n=== {pack.get('label') or pack['pack_id']} ({pack['pack_id']}) ===")
    print(f"{len(answered)} answered of {total} scenarios, across {len({r.get('reviewer') for r in all_records})} reviewer(s)")

    if buckets is not None:
        print(f"restricted to rounds: {', '.join(sorted(buckets))}")

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
    # The agree rate is a SELF-REPORTED label and its usage drifts between
    # rounds (round 1 pressed "close" once in rounds 3-9; round 2 pressed it
    # six times and "disagree" sixteen, while its picks were actually closer
    # to the engine's on every measured axis). The rank/gap numbers below it
    # are computed from the frozen table and carry no such drift, so they are
    # what a cross-round comparison should be built on.
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
        if ranks:
            within3 = sum(1 for k in ranks if k <= 3)
            print(f"reviewer's #1 within the engine's top 3: {within3}/{len(with_choice)} ({100 * within3 / len(with_choice):.0f}%)")
            hist = Counter(ranks)
            spread = "  ".join(f"#{k}:{hist[k]}" for k in sorted(hist))
            print(f"rank of reviewer's #1 in the engine's table: {spread}")
    print("(agree rate is self-reported and drifts between rounds; the rank and gap")
    print(" lines are read off the frozen table and are the comparable ones)")

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
    print_roster_health(answered, pack)

    print("\n--- Positional bias: engine's top rec vs. reviewer's #1 ---")
    matrix: Counter = Counter()
    for r in with_choice:
        g = _top_grade(r)
        engine_pos = r.get("top_rec_position") or "?"
        my_pos = (g or {}).get("position") or "?"
        matrix[(engine_pos, my_pos)] += 1
    # A "none of these" answer names no player, so it has no `graded`
    # block and used to be invisible in exactly the table it belongs in --
    # which is backwards, since "none of these" on a roster missing a
    # mandatory starter is the sharpest thing this tool can collect.
    # `none_position` (web/train.html) carries the position instead.
    none_rows = [
        r for r in answered
        if not r.get("choices") and r.get("none_position")
    ]
    for r in none_rows:
        matrix[(r.get("top_rec_position") or "?", r["none_position"])] += 1
    if matrix:
        positions = sorted({p for pair in matrix for p in pair})
        header = "engine \\ reviewer".ljust(18) + "".join(p.rjust(6) for p in positions)
        print(f"  {header}")
        for ep in positions:
            row = "".join(str(matrix.get((ep, mp), 0)).rjust(6) for mp in positions)
            print(f"  {ep.ljust(18)}{row}")
    if none_rows:
        print(f"  (includes {len(none_rows)} \"none of these\" answer(s), counted by the position named)")
    unnamed = [
        r for r in answered
        if r.get("verdict") == "none" and not r.get("choices") and not r.get("none_position")
    ]
    if unnamed:
        print(f"  ({len(unnamed)} \"none of these\" answer(s) named neither a player nor a")
        print("   position, so they appear in no table here -- their notes are all there is)")

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
        buckets = parse_round_filter(args.rounds)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
        # --reviewer wins over the file's own field, which reviewers routinely
        # leave blank (both returned files so far did), leaving the graded
        # JSONL named after the responses filename instead of a person.
        reviewer = args.reviewer or responses.get("reviewer") or Path(responses_path).stem
        for r in records:
            r["reviewer"] = reviewer
        written = _write_feedback_jsonl(records, pack["pack_id"], reviewer, out_dir, args.append)
        if written is not None:
            print(f"  {responses_path}: {len(records)} graded answers -> {written}")
        all_records.extend(records)

    print_summary(pack, all_records, args.top, buckets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
