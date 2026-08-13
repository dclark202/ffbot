# Contributing

This is shared as a working template, not a maintained product. A few things
that follow from that:

- **PRs are welcome but unmaintained on any schedule.** There's no promise of
  review turnaround.
- **Issues are for bugs, not support.** If your league's scoring doesn't
  import cleanly, or you hit a real crash, open an issue with the specifics.
  "How do I set up my league" belongs in [docs/SETUP.md](docs/SETUP.md), not
  a new issue.
- **Design invariants are in [CLAUDE.md](CLAUDE.md).** If you're changing
  `ffbot/lineup.py`, `ffbot/policy.py`, or anything with a live network seam,
  read that file first — it documents the constraints that keep the
  optimizer exact, drops guarded, and every live fetch fail-safe.
- **Run `pytest` before opening a PR.** No CI is configured; the test suite
  is the whole safety net.

If you fork this for your own league (the expected use — see the README's
"Use this template" instructions), you don't need to follow any of the above.
Fork it, rename it, change whatever you want.
