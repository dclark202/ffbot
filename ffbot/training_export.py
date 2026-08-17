"""Render a training pack (`ffbot/training.py`) as one standalone HTML file.

`web/train.html` is the checked-in template: it loads `/style.css`,
`/common.js`, and `/draft_render.js` the same way every other page in
`web/` does, and carries an empty `<script id="pack" type="application/json">`
placeholder. This module reads that template and the three assets it
references, and stitches them into a single file with no external
references at all -- something a non-technical reviewer can open by
double-clicking, over email or a shared drive, with no server and no
Python involved.

String-replace templating, not a real templating engine, because the
substitution surface is exactly three fixed markers and pulling in a
dependency for three `str.replace` calls would be the wrong trade. Each
marker is asserted to appear exactly once in the template -- if a future
edit to `train.html` accidentally duplicates or removes one, this raises
loudly here rather than silently shipping a page missing its data or its
styling. See `tests/test_training_export.py` for the "no external
references survive" check this relies on instead of eyeballing the output.
"""

from __future__ import annotations

import json
from pathlib import Path

_STYLE_MARKER = '<link rel="stylesheet" href="/style.css">'
_COMMON_JS_MARKER = '<script src="/common.js"></script>'
_DRAFT_RENDER_JS_MARKER = '<script src="/draft_render.js"></script>'
_PACK_MARKER = '<script id="pack" type="application/json"></script>'


class TrainingExportError(ValueError):
    """`train.html` (or one of its assets) doesn't match the expected
    template shape -- see the markers above."""


def _replace_once(html: str, marker: str, replacement: str, what: str) -> str:
    count = html.count(marker)
    if count != 1:
        raise TrainingExportError(
            f"expected exactly one {what} marker in web/train.html, found {count}"
        )
    return html.replace(marker, replacement)


def render_standalone(pack: dict, web_dir: str | Path = "web") -> str:
    """Return the fully self-contained HTML for `pack` -- ready to write to
    a single `.html` file. Pure: reads the template/assets from `web_dir`
    but performs no other I/O and writes nothing itself (mirrors
    `training.build_pack`'s "builders don't write" split)."""
    web_dir = Path(web_dir)
    template = (web_dir / "train.html").read_text(encoding="utf-8")
    style_css = (web_dir / "style.css").read_text(encoding="utf-8")
    common_js = (web_dir / "common.js").read_text(encoding="utf-8")
    draft_render_js = (web_dir / "draft_render.js").read_text(encoding="utf-8")

    html = template
    html = _replace_once(html, _STYLE_MARKER, f"<style>\n{style_css}\n</style>", "style.css")
    html = _replace_once(
        html, _COMMON_JS_MARKER, f"<script>\n{common_js}\n</script>", "common.js",
    )
    html = _replace_once(
        html, _DRAFT_RENDER_JS_MARKER, f"<script>\n{draft_render_js}\n</script>", "draft_render.js",
    )
    # `separators=(",", ":")` keeps this compact -- a 30-scenario pack's
    # embedded JSON is most of the file's size, and no human reads it raw.
    pack_json = json.dumps(pack, default=str, separators=(",", ":"))
    # `</script` inside a JSON string value (a player note containing that
    # literal text) would otherwise close the tag early and truncate the
    # embedded data -- escape it the standard way for JSON-in-HTML.
    pack_json = pack_json.replace("</script", "<\\/script")
    html = _replace_once(
        html, _PACK_MARKER, f'<script id="pack" type="application/json">{pack_json}</script>', "pack data",
    )
    return html


def write_standalone(pack: dict, out_path: str | Path, web_dir: str | Path = "web") -> Path:
    """Render and write the standalone file, creating the directory. The
    only I/O in this module -- kept separate from `render_standalone` so
    the render itself stays testable without touching disk."""
    html = render_standalone(pack, web_dir)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")
    return p
