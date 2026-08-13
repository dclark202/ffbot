from __future__ import annotations

import time

import pytest

from ffbot.reports_index import ReportNotFoundError, list_reports, read_report


class TestListReports:
    def test_missing_directory_returns_empty_list(self, tmp_path):
        assert list_reports(tmp_path / "nope") == []

    def test_lists_md_files_only(self, tmp_path):
        (tmp_path / "a.md").write_text("A", encoding="utf-8")
        (tmp_path / "b.txt").write_text("B", encoding="utf-8")
        out = list_reports(tmp_path)
        assert [r.filename for r in out] == ["a.md"]

    def test_newest_first(self, tmp_path):
        (tmp_path / "old.md").write_text("old", encoding="utf-8")
        time.sleep(0.01)
        (tmp_path / "new.md").write_text("new", encoding="utf-8")
        out = list_reports(tmp_path)
        assert [r.filename for r in out] == ["new.md", "old.md"]

    def test_size_reported(self, tmp_path):
        (tmp_path / "a.md").write_text("hello", encoding="utf-8")
        out = list_reports(tmp_path)
        assert out[0].size == 5

    def test_subdirectories_ignored(self, tmp_path):
        sub = tmp_path / "sub.md"
        sub.mkdir()
        out = list_reports(tmp_path)
        assert out == []


class TestReadReport:
    def test_reads_content(self, tmp_path):
        (tmp_path / "a.md").write_text("# Report\ncontent", encoding="utf-8")
        assert read_report(tmp_path, "a.md") == "# Report\ncontent"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, "nope.md")

    def test_path_traversal_with_slash_rejected(self, tmp_path):
        secret = tmp_path.parent / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, "../secret.txt")

    def test_path_traversal_with_backslash_rejected(self, tmp_path):
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, "..\\secret.txt")

    def test_absolute_path_rejected(self, tmp_path):
        (tmp_path / "real.md").write_text("x", encoding="utf-8")
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, str(tmp_path / "real.md"))

    def test_empty_filename_rejected(self, tmp_path):
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, "")

    def test_dot_dot_alone_rejected(self, tmp_path):
        with pytest.raises(ReportNotFoundError):
            read_report(tmp_path, "..")
