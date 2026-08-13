from __future__ import annotations

from ffbot.config import LeagueScoring, TeamStanding
from ffbot.sleeper_standings import fetch_standings, merge_standings


class FakeClient:
    def __init__(self, rosters=None, users=None, matchups=None):
        self._rosters = rosters or []
        self._users = users or []
        self._matchups = matchups or []

    def rosters(self, league_id):
        return self._rosters

    def league_users(self, league_id):
        return self._users

    def matchups(self, league_id, week):
        return self._matchups


def _roster(roster_id, owner_id, wins=0, losses=0, ties=0, fpts=0.0, waiver_position=None):
    settings = {"wins": wins, "losses": losses, "ties": ties, "fpts": fpts}
    if waiver_position is not None:
        settings["waiver_position"] = waiver_position
    return {"roster_id": roster_id, "owner_id": owner_id, "settings": settings}


def _user(user_id, team_name=None, display_name=None):
    return {"user_id": user_id, "display_name": display_name, "metadata": {"team_name": team_name} if team_name else {}}


class TestFetchStandings:
    def test_team_names_from_metadata_team_name(self):
        client = FakeClient(
            rosters=[_roster(1, "u1", wins=5)],
            users=[_user("u1", team_name="The Team Name")],
        )
        teams, my_team, my_opp = fetch_standings(client, "L1", week=3)
        assert teams[0].name == "The Team Name"

    def test_falls_back_to_display_name_then_owner_id(self):
        client = FakeClient(
            rosters=[_roster(1, "u1"), _roster(2, "u2"), _roster(3, "u3")],
            users=[_user("u1", display_name="Manager1"), _user("u2")],
        )
        teams, _, _ = fetch_standings(client, "L1", week=3)
        names = {t.name for t in teams}
        assert "Manager1" in names
        assert "u2" in names  # no team_name, no display_name -- owner_id itself
        assert "roster 3" in names  # no matching user row at all

    def test_record_string_includes_ties_only_when_nonzero(self):
        client = FakeClient(
            rosters=[_roster(1, "u1", wins=7, losses=3), _roster(2, "u2", wins=5, losses=5, ties=2)],
            users=[_user("u1", "A"), _user("u2", "B")],
        )
        teams, _, _ = fetch_standings(client, "L1", week=3)
        by_name = {t.name: t for t in teams}
        assert by_name["A"].record == "7-3"
        assert by_name["B"].record == "5-5-2"

    def test_seed_ranks_by_wins_then_points(self):
        client = FakeClient(
            rosters=[
                _roster(1, "u1", wins=5, fpts=500.0),
                _roster(2, "u2", wins=7, fpts=400.0),
                _roster(3, "u3", wins=5, fpts=600.0),
            ],
            users=[_user("u1", "A"), _user("u2", "B"), _user("u3", "C")],
        )
        teams, _, _ = fetch_standings(client, "L1", week=3)
        by_name = {t.name: t for t in teams}
        assert by_name["B"].seed == 1  # most wins
        assert by_name["C"].seed == 2  # tied on wins, more points
        assert by_name["A"].seed == 3

    def test_waiver_priority_from_settings(self):
        client = FakeClient(rosters=[_roster(1, "u1", waiver_position=3)], users=[_user("u1", "A")])
        teams, _, _ = fetch_standings(client, "L1", week=3)
        assert teams[0].waiver_priority == 3

    def test_eliminated_always_false(self):
        client = FakeClient(rosters=[_roster(1, "u1")], users=[_user("u1", "A")])
        teams, _, _ = fetch_standings(client, "L1", week=3)
        assert teams[0].eliminated is False

    def test_no_roster_id_leaves_my_team_and_opponent_empty(self):
        client = FakeClient(rosters=[_roster(1, "u1")], users=[_user("u1", "A")])
        _, my_team, my_opponent = fetch_standings(client, "L1", week=3, my_roster_id=None)
        assert my_team == "" and my_opponent == ""

    def test_my_team_and_opponent_resolved_from_matchups(self):
        client = FakeClient(
            rosters=[_roster(1, "u1"), _roster(2, "u2"), _roster(3, "u3")],
            users=[_user("u1", "A"), _user("u2", "B"), _user("u3", "C")],
            matchups=[
                {"roster_id": 1, "matchup_id": 10},
                {"roster_id": 2, "matchup_id": 10},
                {"roster_id": 3, "matchup_id": 20},
            ],
        )
        _, my_team, my_opponent = fetch_standings(client, "L1", week=3, my_roster_id=1)
        assert my_team == "A"
        assert my_opponent == "B"

    def test_unresolvable_matchup_leaves_opponent_empty(self):
        client = FakeClient(
            rosters=[_roster(1, "u1")],
            users=[_user("u1", "A")],
            matchups=[],  # no matchup data for this week
        )
        _, my_team, my_opponent = fetch_standings(client, "L1", week=3, my_roster_id=1)
        assert my_team == "A"
        assert my_opponent == ""


class TestMergeStandings:
    def test_live_teams_added_when_league_yml_has_none(self):
        league = LeagueScoring()
        teams = [TeamStanding(name="A", record="5-3", seed=1)]
        merged = merge_standings(league, teams, my_team_name="A", my_opponent_name="B", week=5)
        assert merged.teams == teams
        assert merged.my_team == "A"
        assert merged.my_opponent == "B"
        assert merged.week == 5

    def test_hand_curated_team_entry_wins_outright(self):
        hand_typed = TeamStanding(name="A", record="9-0", seed=99)  # deliberately different from live
        league = LeagueScoring(teams=[hand_typed])
        live_teams = [TeamStanding(name="A", record="5-3", seed=1), TeamStanding(name="B", record="4-4", seed=2)]
        merged = merge_standings(league, live_teams, my_team_name="A", my_opponent_name="B", week=5)
        by_name = {t.name: t for t in merged.teams}
        assert by_name["A"] == hand_typed  # untouched
        assert by_name["B"].record == "4-4"  # live team added since league.yml had none

    def test_hand_typed_my_team_and_opponent_win_over_live(self):
        league = LeagueScoring(my_team="Hand Typed", my_opponent="Also Hand Typed")
        merged = merge_standings(league, [], my_team_name="Live A", my_opponent_name="Live B", week=5)
        assert merged.my_team == "Hand Typed"
        assert merged.my_opponent == "Also Hand Typed"

    def test_hand_typed_week_wins_over_live(self):
        league = LeagueScoring(week=3)
        merged = merge_standings(league, [], my_team_name="", my_opponent_name="", week=9)
        assert merged.week == 3

    def test_scoring_rules_untouched_by_standings_merge(self):
        league = LeagueScoring(name="My League")
        original_passing = league.passing
        merged = merge_standings(league, [TeamStanding(name="A")], "A", "", week=1)
        assert merged.passing == original_passing
        assert merged.name == "My League"
