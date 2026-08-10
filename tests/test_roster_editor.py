from __future__ import annotations

from ffbot import roster_editor as re_


class TestRosterEntriesJson:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert re_.roster_entries_json(tmp_path / "roster.yml") == []

    def test_bare_names_round_trip(self, tmp_path):
        p = tmp_path / "roster.yml"
        p.write_text("players:\n  - Josh Allen\n  - Ja'Marr Chase\n", encoding="utf-8")
        out = re_.roster_entries_json(p)
        assert [e["name"] for e in out] == ["Josh Allen", "Ja'Marr Chase"]
        assert out[0]["undroppable"] is False
        assert out[0]["keeper_round"] is None

    def test_flags_surfaced(self, tmp_path):
        p = tmp_path / "roster.yml"
        p.write_text(
            "players:\n"
            "  - name: Josh Allen\n"
            "    undroppable: true\n"
            "    keeper_round: 3\n"
            "    acquired: draft\n"
            "    note: my guy\n"
            "    blocking: true\n",
            encoding="utf-8",
        )
        out = re_.roster_entries_json(p)
        assert out[0] == {
            "name": "Josh Allen",
            "undroppable": True,
            "keeper_round": 3,
            "acquired": "draft",
            "note": "my guy",
            "blocking": True,
        }


class TestWriteRosterEntries:
    def test_plain_entry_written_as_bare_string(self, tmp_path):
        p = tmp_path / "roster.yml"
        re_.write_roster_entries(p, [{"name": "Josh Allen"}])
        assert "- Josh Allen" in p.read_text(encoding="utf-8")

    def test_flagged_entry_written_as_mapping(self, tmp_path):
        p = tmp_path / "roster.yml"
        re_.write_roster_entries(p, [{"name": "Josh Allen", "undroppable": True}])
        text = p.read_text(encoding="utf-8")
        assert "name: Josh Allen" in text
        assert "undroppable: true" in text

    def test_round_trip_preserves_flags(self, tmp_path):
        p = tmp_path / "roster.yml"
        entries = [
            {"name": "Josh Allen", "undroppable": True, "keeper_round": 2},
            {"name": "Puka Nacua"},
        ]
        re_.write_roster_entries(p, entries)
        reloaded = re_.roster_entries_json(p)
        assert reloaded[0]["name"] == "Josh Allen"
        assert reloaded[0]["undroppable"] is True
        assert reloaded[0]["keeper_round"] == 2
        assert reloaded[1]["name"] == "Puka Nacua"
        assert reloaded[1]["undroppable"] is False

    def test_blank_name_entries_are_dropped(self, tmp_path):
        p = tmp_path / "roster.yml"
        re_.write_roster_entries(p, [{"name": "  "}, {"name": "Josh Allen"}])
        reloaded = re_.roster_entries_json(p)
        assert [e["name"] for e in reloaded] == ["Josh Allen"]

    def test_creates_parent_directories(self, tmp_path):
        p = tmp_path / "nested" / "roster.yml"
        re_.write_roster_entries(p, [{"name": "Josh Allen"}])
        assert p.exists()
