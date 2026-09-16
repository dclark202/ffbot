"""Make a terminal that cannot represent this repo's output survive it.

Every renderer here writes prose with typographic characters -- an em dash
between a move and its reason, `→` between two lineup slots, `Σ`/`Π` in the
draft table's headers, `≈` before an approximation, `Δ` before a delta. On a
Windows console still running a legacy code page, `sys.stdout` is opened as
cp1252 with `errors="strict"`, and the first `→` raises
`UnicodeEncodeError` *at print time* -- after the whole report has been
computed, every live fetch has been spent, and the recommendation log has
already been written to disk. The run does all of its work and then dies on
the last line, which is the worst possible place to fail.

`sys.stderr` never had this problem: Python opens it with
`errors="backslashreplace"` precisely so a crash report can always be
printed. That asymmetry is the whole bug -- the warnings got through and the
report did not.

Two deliberate choices:

1. **The stream's encoding is left alone.** Forcing UTF-8 onto a console
   genuinely running cp1252 does not fix anything; it turns every em dash --
   a character cp1252 *can* represent -- into mojibake. Only the ERROR
   HANDLER changes, so text the terminal can already show is untouched.

2. **Unrepresentable characters degrade to meaningful ASCII, not to `?`.**
   "RB ? W/R/T" is not better than a crash by much. `_ASCII_FALLBACKS` maps
   the handful this repo actually emits; anything not in it still degrades
   safely (to `?`) rather than raising, so a new character added later
   costs legibility on one console, never a run.

This is a terminal concern, so nothing in the engine calls it -- each CLI
entry point does, once, at the top of `main()`.
"""

from __future__ import annotations

import codecs
import sys

# The characters this repo emits that cp1252 cannot represent, and what to
# show instead. Deliberately meaning-preserving: a reader on a legacy
# console should still be able to act on the line.
_ASCII_FALLBACKS = {
    "→": "->",    # → between two lineup slots
    "≈": "~",     # ≈ before an approximation
    "−": "-",     # − a real minus sign
    "Δ": "d",     # Δ a delta
    "Σ": "sum",   # Σ a total
    "Π": "prod",  # Π a product
    "—": "--",    # — em dash (cp1252 HAS this; here for stricter encodings)
    "–": "-",     # – en dash, likewise
    "·": "*",     # · middle dot, likewise
}

_HANDLER_NAME = "ffbot.ascii_fallback"


def _ascii_fallback(exc: UnicodeEncodeError) -> tuple[str, int]:
    """Codec error handler: replace what the stream cannot encode.

    Registered rather than applied by hand so it works for EVERY write to
    the stream, including ones this repo does not own (a traceback, a
    library's warning), which is the point -- a guard that only covers the
    calls we remembered is not a guard.
    """
    chunk = exc.object[exc.start:exc.end]
    return "".join(_ASCII_FALLBACKS.get(ch, "?") for ch in chunk), exc.end


codecs.register_error(_HANDLER_NAME, _ascii_fallback)


def make_streams_safe(*streams) -> None:
    """Point `stdout`/`stderr` at the fallback handler. Idempotent.

    Called at the top of a CLI's `main()`. Silently does nothing when a
    stream cannot be reconfigured -- a pipe, a captured buffer under
    pytest, an already-detached stream -- because a console convenience
    must never be the reason a run fails.
    """
    for stream in (streams or (sys.stdout, sys.stderr)):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors=_HANDLER_NAME)
        except (ValueError, OSError, AttributeError):
            # Detached, already closed, or not a text stream. Not worth a
            # warning: the run's real output is unaffected either way.
            continue
