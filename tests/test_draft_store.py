from __future__ import annotations

import pytest

from ffbot.draft_store import (
    DraftStoreError,
    archive_log,
    list_snapshots,
    load_snapshot,
    save_snapshot,
)


class TestArchiveLog:
    def test_missing_log_returns_none(self, tmp_path):
        assert archive_log(tmp_path / "draft_log.jsonl") is None

    def test_renames_to_a_timestamped_sibling(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text('{"line": "mahomes"}\n', encoding="utf-8")
        archived = archive_log(log_path)
        assert archived is not None
        assert not log_path.exists()
        assert archived.exists()
        assert archived.name.startswith("draft_log.")
        assert archived.suffix == ".jsonl"
        assert archived.read_text(encoding="utf-8") == '{"line": "mahomes"}\n'


class TestSaveSnapshot:
    def test_missing_log_raises(self, tmp_path):
        with pytest.raises(DraftStoreError, match="nothing to save"):
            save_snapshot(tmp_path / "draft_log.jsonl", "mydraft", saves_dir=tmp_path / "saves")

    def test_copies_log_to_saves_dir(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text('{"line": "chase"}\n', encoding="utf-8")
        saves_dir = tmp_path / "saves"
        dest = save_snapshot(log_path, "mydraft", saves_dir=saves_dir)
        assert dest == saves_dir / "mydraft.jsonl"
        assert dest.read_text(encoding="utf-8") == '{"line": "chase"}\n'
        # Original log is untouched -- save is a copy, not a move.
        assert log_path.exists()

    def test_overwrites_prior_save_of_the_same_name(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        saves_dir = tmp_path / "saves"
        log_path.write_text('{"line": "a"}\n', encoding="utf-8")
        save_snapshot(log_path, "mydraft", saves_dir=saves_dir)
        log_path.write_text('{"line": "a"}\n{"line": "b"}\n', encoding="utf-8")
        dest = save_snapshot(log_path, "mydraft", saves_dir=saves_dir)
        assert dest.read_text(encoding="utf-8") == '{"line": "a"}\n{"line": "b"}\n'

    @pytest.mark.parametrize("bad_name", ["", "  ", "../escape", "a/b", "a\\b", "."])
    def test_rejects_unsafe_names(self, tmp_path, bad_name):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text("{}\n", encoding="utf-8")
        with pytest.raises(DraftStoreError):
            save_snapshot(log_path, bad_name, saves_dir=tmp_path / "saves")


class TestListSnapshots:
    def test_no_saves_dir_returns_empty(self, tmp_path):
        assert list_snapshots(tmp_path / "saves") == []

    def test_lists_save_names_sorted(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text("{}\n", encoding="utf-8")
        saves_dir = tmp_path / "saves"
        save_snapshot(log_path, "zeta", saves_dir=saves_dir)
        save_snapshot(log_path, "alpha", saves_dir=saves_dir)
        assert list_snapshots(saves_dir) == ["alpha", "zeta"]


class TestLoadSnapshot:
    def test_unknown_name_raises_with_hint(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text("{}\n", encoding="utf-8")
        saves_dir = tmp_path / "saves"
        save_snapshot(log_path, "mydraft", saves_dir=saves_dir)
        with pytest.raises(DraftStoreError, match="mydraft"):
            load_snapshot("nope", saves_dir=saves_dir)

    def test_unknown_name_with_no_saves_at_all(self, tmp_path):
        with pytest.raises(DraftStoreError, match="no saves exist"):
            load_snapshot("nope", saves_dir=tmp_path / "saves")

    def test_returns_path_to_the_saved_log(self, tmp_path):
        log_path = tmp_path / "draft_log.jsonl"
        log_path.write_text('{"line": "jefferson"}\n', encoding="utf-8")
        saves_dir = tmp_path / "saves"
        save_snapshot(log_path, "mydraft", saves_dir=saves_dir)
        loaded = load_snapshot("mydraft", saves_dir=saves_dir)
        assert loaded.read_text(encoding="utf-8") == '{"line": "jefferson"}\n'
