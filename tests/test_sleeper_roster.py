from __future__ import annotations

import pytest

from ffbot.models import Player
from ffbot.roster_source import RosterEntry
from ffbot.sleeper_roster import (
    RosterSourceError,
    SleeperRosterPlayer,
    apply_sleeper_identity,
    fetch_my_roster,
    merge_flags,
    resolve_roster_id,
    resolve_starters_slot_map,
    starters_slot_map,
    waiver_position,
)


class FakeClient:
    def __init__(self, rosters=None, users=None):
        self._rosters = rosters or []
        self._users = users or {}
        self.rosters_calls = []

    def user(self, username):
        return self._users.get(username)

    def rosters(self, league_id, **kwargs):
        self.rosters_calls.append(kwargs)
        return self._rosters


class TestResolveRosterId:
    def test_resolves_by_owner_id(self):
        client = FakeClient(
            rosters=[{"roster_id": 4, "owner_id": "u1"}, {"roster_id": 5, "owner_id": "u2"}],
            users={"duncan": {"user_id": "u1"}},
        )
        assert resolve_roster_id(client, "L1", "duncan") == 4

    def test_unknown_username_raises(self):
        client = FakeClient()
        with pytest.raises(RosterSourceError, match="no Sleeper user"):
            resolve_roster_id(client, "L1", "nobody")

    def test_username_with_no_roster_in_league_raises(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "someone_else"}], users={"duncan": {"user_id": "u1"}})
        with pytest.raises(RosterSourceError, match="no roster owned"):
            resolve_roster_id(client, "L1", "duncan")

    def test_ttl_override_passed_through_to_client(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1"}], users={"duncan": {"user_id": "u1"}})
        resolve_roster_id(client, "L1", "duncan", roster_ttl_minutes=15.0)
        assert client.rosters_calls == [{"ttl_minutes": 15.0}]

    def test_no_ttl_override_passes_no_kwargs(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1"}], users={"duncan": {"user_id": "u1"}})
        resolve_roster_id(client, "L1", "duncan")
        assert client.rosters_calls == [{}]


class TestFetchMyRoster:
    PLAYERS = {
        "4046": {"full_name": "Patrick Mahomes", "position": "QB", "team": "KC", "injury_status": None},
        "11533": {"full_name": "Brandon Aubrey", "position": "K", "team": "DAL", "injury_status": "Questionable"},
        "KC": {"first_name": "Kansas City", "last_name": "Chiefs", "position": "DEF", "team": "KC", "injury_status": None},
    }

    def test_resolves_names_team_position_status(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046", "11533"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS)
        by_id = {p.sleeper_id: p for p in roster}
        assert by_id["4046"].name == "Patrick Mahomes"
        assert by_id["4046"].team == "KC"
        assert by_id["4046"].status == ""
        assert by_id["11533"].status == "Q"

    def test_defense_resolves_from_first_last_name(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["KC"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS)
        assert roster[0].name == "Kansas City Chiefs"
        assert roster[0].position == "DEF"

    def test_unknown_roster_id_raises(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": []}])
        with pytest.raises(RosterSourceError, match="roster_id 99"):
            fetch_my_roster(client, "L1", 99, self.PLAYERS)

    def test_player_id_not_in_dump_is_skipped_not_crashed_on(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046", "99999"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS)
        assert len(roster) == 1

    def test_ownership_joined_when_given(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS, ownership={"4046": {"owned": 99.2, "started": 96.3}})
        assert roster[0].percent_owned == 99.2
        assert roster[0].started_pct == 96.3

    def test_no_ownership_arg_leaves_percent_owned_none(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS)
        assert roster[0].percent_owned is None

    def test_ttl_override_passed_through_to_client(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": []}])
        fetch_my_roster(client, "L1", 4, self.PLAYERS, roster_ttl_minutes=15.0)
        assert client.rosters_calls == [{"ttl_minutes": 15.0}]

    def test_slot_map_fills_slot_per_player(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046", "11533"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS, slot_map={"4046": "QB"})
        by_id = {p.sleeper_id: p for p in roster}
        assert by_id["4046"].slot == "QB"
        assert by_id["11533"].slot == ""  # unmapped -- unknown, not benched

    def test_no_slot_map_leaves_slot_empty(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "players": ["4046"]}])
        roster = fetch_my_roster(client, "L1", 4, self.PLAYERS)
        assert roster[0].slot == ""


class TestStartersSlotMap:
    def test_zips_starters_against_non_bench_slots_in_order(self):
        layout = ["QB", "RB", "WR", "BN", "BN"]
        mapping, warnings = starters_slot_map(layout, ["100", "200", "300"])
        assert mapping == {"100": "QB", "200": "RB", "300": "WR"}
        assert warnings == []

    def test_zero_placeholder_skipped(self):
        layout = ["QB", "RB"]
        mapping, warnings = starters_slot_map(layout, ["100", "0"])
        assert mapping == {"100": "QB"}
        assert warnings == []

    def test_reserve_maps_to_ir(self):
        layout = ["QB"]
        mapping, _ = starters_slot_map(layout, ["100"], reserve=["200"])
        assert mapping["200"] == "IR"

    def test_taxi_maps_to_taxi(self):
        layout = ["QB"]
        mapping, _ = starters_slot_map(layout, ["100"], taxi=["300"])
        assert mapping["300"] == "TAXI"

    def test_reserve_and_taxi_zero_placeholders_skipped(self):
        layout = ["QB"]
        mapping, _ = starters_slot_map(layout, ["100"], reserve=["0"], taxi=["0"])
        assert mapping == {"100": "QB"}

    def test_length_mismatch_warns_but_still_zips_what_it_can(self):
        layout = ["QB", "RB", "WR"]
        mapping, warnings = starters_slot_map(layout, ["100", "200"])
        assert mapping == {"100": "QB", "200": "RB"}
        assert len(warnings) == 1
        assert "misaligned" in warnings[0]

    def test_empty_starters_is_a_noop(self):
        mapping, warnings = starters_slot_map(["QB"], [])
        assert mapping == {}
        assert len(warnings) == 1  # 1 starter slot vs. 0 starters is still a mismatch

    def test_bench_and_ir_slots_excluded_from_the_zip(self):
        layout = ["QB", "BN", "BN", "IR"]
        mapping, warnings = starters_slot_map(layout, ["100"])
        assert mapping == {"100": "QB"}
        assert warnings == []


class TestResolveStartersSlotMap:
    def test_resolves_from_a_live_rosters_call(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "starters": ["100"], "reserve": [], "taxi": []}])
        mapping, warnings = resolve_starters_slot_map(client, "L1", 4, ["QB"])
        assert mapping == {"100": "QB"}
        assert warnings == []

    def test_unknown_roster_id_degrades_quietly(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "starters": []}])
        mapping, warnings = resolve_starters_slot_map(client, "L1", 99, ["QB"])
        assert mapping == {}
        assert warnings == []

    def test_ttl_override_passed_through_to_client(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "starters": []}])
        resolve_starters_slot_map(client, "L1", 4, ["QB"], roster_ttl_minutes=15.0)
        assert client.rosters_calls == [{"ttl_minutes": 15.0}]


class TestWaiverPosition:
    def test_reads_waiver_position_off_the_roster(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "settings": {"waiver_position": 6}}])
        assert waiver_position(client, "L1", 4) == 6

    def test_unknown_roster_id_returns_none_not_an_error(self):
        # fetch_my_roster raises for this same case -- waiver_position is
        # only ever called after that already succeeded (see report.py), so
        # a caller that somehow reaches this on a bad roster_id gets a
        # gentle "no priority known" rather than a second exception path to
        # handle for the same misconfiguration.
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "settings": {"waiver_position": 6}}])
        assert waiver_position(client, "L1", 99) is None

    def test_missing_settings_block_returns_none(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1"}])
        assert waiver_position(client, "L1", 4) is None

    def test_missing_waiver_position_field_returns_none(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "settings": {}}])
        assert waiver_position(client, "L1", 4) is None

    def test_ttl_override_passed_through_to_client(self):
        client = FakeClient(rosters=[{"roster_id": 4, "owner_id": "u1", "settings": {"waiver_position": 1}}])
        waiver_position(client, "L1", 4, roster_ttl_minutes=15.0)
        assert client.rosters_calls == [{"ttl_minutes": 15.0}]


class TestMergeFlags:
    def test_no_roster_yml_leaves_every_flag_default(self, tmp_path):
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="")]
        entries = merge_flags(sleeper_players, tmp_path / "does_not_exist.yml")
        assert entries == [RosterEntry(name="Josh Allen")]

    def test_matching_roster_yml_entry_flags_carry_through(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text(
            "players:\n  - name: Josh Allen\n    undroppable: true\n    keeper_round: 2\n    note: my guy\n",
            encoding="utf-8",
        )
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="")]
        [entry] = merge_flags(sleeper_players, path)
        assert entry.undroppable is True
        assert entry.keeper_round == 2
        assert entry.note == "my guy"

    def test_roster_yml_entry_for_a_player_not_on_the_live_roster_is_ignored(self, tmp_path):
        path = tmp_path / "roster.yml"
        path.write_text("players:\n  - name: Someone Traded Away\n    undroppable: true\n", encoding="utf-8")
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="")]
        entries = merge_flags(sleeper_players, path)
        assert [e.name for e in entries] == ["Josh Allen"]
        assert entries[0].undroppable is False

    def test_output_order_matches_sleeper_roster_order(self, tmp_path):
        sleeper_players = [
            SleeperRosterPlayer(sleeper_id="1", name="B Player", team="X", position="WR", status=""),
            SleeperRosterPlayer(sleeper_id="2", name="A Player", team="X", position="WR", status=""),
        ]
        entries = merge_flags(sleeper_players, tmp_path / "no.yml")
        assert [e.name for e in entries] == ["B Player", "A Player"]


class TestApplySleeperIdentity:
    def test_status_and_ownership_set(self):
        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"])]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="Q", percent_owned=99.5)]
        [out] = apply_sleeper_identity(players, sleeper_players)
        assert out.status == "Q"
        assert out.percent_owned == 99.5

    def test_player_not_in_sleeper_list_passes_through_unchanged(self):
        players = [Player(player_id=1, name="Unknown Guy", eligible_positions=["WR"], status="")]
        out = apply_sleeper_identity(players, [])
        assert out[0].status == ""
        assert out[0].percent_owned is None

    def test_weekly_override_can_still_win_afterward(self):
        # Simulates the real pipeline: apply_sleeper_identity sets the base
        # layer, then week.apply_status_overrides (unchanged, tested
        # separately) still wins if the weekly file has an entry.
        from dataclasses import replace as _replace

        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"])]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="Q")]
        base = apply_sleeper_identity(players, sleeper_players)
        assert base[0].status == "Q"
        overridden = [_replace(base[0], status="O")]  # what a weekly override would do
        assert overridden[0].status == "O"

    def test_known_slot_sets_selected_position_when_roster_positions_given(self):
        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"], selected_position="BN")]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="", slot="QB")]
        [out] = apply_sleeper_identity(players, sleeper_players, roster_positions={"QB": 1, "BN": 5})
        assert out.selected_position == "QB"

    def test_sleeper_flex_slot_translated_onto_layout_spelling(self):
        players = [Player(player_id=1, name="CeeDee Lamb", eligible_positions=["WR"], selected_position="BN")]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="CeeDee Lamb", team="DAL", position="WR", status="", slot="FLEX")]
        [out] = apply_sleeper_identity(players, sleeper_players, roster_positions={"WR": 2, "W/R/T": 1, "BN": 5})
        assert out.selected_position == "W/R/T"

    def test_empty_slot_leaves_selected_position_untouched(self):
        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"], selected_position="BN")]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="")]
        [out] = apply_sleeper_identity(players, sleeper_players, roster_positions={"QB": 1, "BN": 5})
        assert out.selected_position == "BN"

    def test_no_roster_positions_leaves_selected_position_untouched(self):
        players = [Player(player_id=1, name="Josh Allen", eligible_positions=["QB"], selected_position="BN")]
        sleeper_players = [SleeperRosterPlayer(sleeper_id="1", name="Josh Allen", team="BUF", position="QB", status="", slot="QB")]
        [out] = apply_sleeper_identity(players, sleeper_players)
        assert out.selected_position == "BN"
