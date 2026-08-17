from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ffbot.training_export import TrainingExportError, render_standalone, write_standalone

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_SIMPLE_PACK = {
    "pack_id": "smoke",
    "label": "Smoke Pack",
    "blind": False,
    "scenarios": [
        {
            "id": "s1",
            "round": 1,
            "pick": 1,
            "round_bucket": "R1-2",
            "top_rec_position": "RB",
            "state": {
                "header": {"pick": 1, "round": 1, "slot_on_clock": 1, "my_slot": 1, "survival_to_pick": None, "on_the_clock": True},
                "recommendations": [],
                "confidence": {},
                "roster": [],
                "draft_log": [],
                "opponents": [],
                "alerts": [],
                "needs_between": {},
                "demand_ahead": {},
            },
        }
    ],
}


@pytest.mark.skipif(not (WEB_DIR / "train.html").exists(), reason="web/train.html not present")
class TestRenderStandalone:
    def test_no_external_references_survive(self):
        html = render_standalone(_SIMPLE_PACK, WEB_DIR)
        # No page asset should be fetched over the network -- everything
        # that was `/style.css`, `/common.js`, `/draft_render.js` must be
        # inlined. A bare `src="/` or `href="/` left behind means the
        # exported file is not actually self-contained.
        assert 'src="/' not in html
        assert 'href="/' not in html
        assert "<link" not in html  # the stylesheet link itself must be gone, replaced by <style>

    def test_pack_json_round_trips(self):
        html = render_standalone(_SIMPLE_PACK, WEB_DIR)
        m = re.search(
            r'<script id="pack" type="application/json">(.*?)</script>', html, re.DOTALL,
        )
        assert m is not None, "embedded pack script tag not found"
        embedded = json.loads(m.group(1))
        assert embedded == _SIMPLE_PACK

    def test_closing_script_tag_inside_pack_data_is_escaped(self):
        pack = json.loads(json.dumps(_SIMPLE_PACK))
        pack["scenarios"][0]["note_with_tag"] = "</script>alert(1)"
        html = render_standalone(pack, WEB_DIR)
        # The literal sequence must never appear unescaped inside the
        # embedded JSON, or it closes the script tag early and truncates
        # (or corrupts) the page's own data.
        pack_section = html[html.index('<script id="pack"'):]
        assert "</script>alert(1)" not in pack_section

    def test_contains_inlined_style_and_scripts(self):
        html = render_standalone(_SIMPLE_PACK, WEB_DIR)
        assert "<style>" in html
        # draft_render.js defines this function; common.js defines `el`.
        assert "function recRow" in html
        assert "function el(" in html

    def test_write_standalone_creates_parent_dirs(self, tmp_path):
        out = write_standalone(_SIMPLE_PACK, tmp_path / "nested" / "pack.html", WEB_DIR)
        assert out.exists()
        assert "function recRow" in out.read_text(encoding="utf-8")


class TestMissingMarkers:
    def test_raises_when_a_marker_is_missing(self, tmp_path):
        web_dir = tmp_path / "web"
        web_dir.mkdir()
        (web_dir / "train.html").write_text("<html><body>no markers here</body></html>", encoding="utf-8")
        (web_dir / "style.css").write_text("body {}", encoding="utf-8")
        (web_dir / "common.js").write_text("function el(){}", encoding="utf-8")
        (web_dir / "draft_render.js").write_text("function recRow(){}", encoding="utf-8")
        with pytest.raises(TrainingExportError):
            render_standalone(_SIMPLE_PACK, web_dir)

    def test_raises_when_a_marker_is_duplicated(self, tmp_path):
        web_dir = tmp_path / "web"
        web_dir.mkdir()
        marker = '<link rel="stylesheet" href="/style.css">'
        (web_dir / "train.html").write_text(
            f"<html><head>{marker}{marker}"
            '<script src="/common.js"></script>'
            '<script src="/draft_render.js"></script>'
            '<script id="pack" type="application/json"></script>'
            "</head><body></body></html>",
            encoding="utf-8",
        )
        (web_dir / "style.css").write_text("body {}", encoding="utf-8")
        (web_dir / "common.js").write_text("function el(){}", encoding="utf-8")
        (web_dir / "draft_render.js").write_text("function recRow(){}", encoding="utf-8")
        with pytest.raises(TrainingExportError):
            render_standalone(_SIMPLE_PACK, web_dir)
