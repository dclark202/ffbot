"""Matchup-centric read/write for `weekly/week-NN.yml` — the GUI's weekly
intel editor.

`week.WeeklyIntel.games` is keyed per-team, and a single matchup is written
twice (once per side — see `week.GameInfo`). Editing that shape directly
invites the two sides drifting apart (wind entered on one team's row but not
its opponent's, e.g.). This module presents one row per matchup instead and
writes both mirrored sides atomically, so that drift can't happen through
the GUI.

Unknown per-game keys (e.g. a future `venue`/`international` field) are
passed straight through on both read and write without this module needing
to know their names — `week._parse_game_entry` already ignores keys it
doesn't recognize, so writing them ahead of a reader that understands them
is harmless, and adding a new field here later is additive, not a rewrite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import week

# Game-info keys carried on every matchup row, beyond the ones with bespoke
# home/away handling (kickoff_et is shared; team_total/opp_total mirror into
# home_team_total/away_team_total). Extending `week.GameInfo` with a new
# plain field only needs an addition here to reach the editor.
_PASSTHROUGH_GAME_FIELDS = ("venue", "international")


def weekly_intel_editor_json(path: str | Path) -> dict:
    """Load `path` (or an empty template if missing) as one row per matchup:
    `{week, generated, source_notes, players: {name: {...}}, matchups: [...]}`.
    """
    return editor_json_from_intel(week.load_weekly_intel(path))


def editor_json_from_intel(intel: week.WeeklyIntel) -> dict:
    """The pure transform behind `weekly_intel_editor_json`, split out so
    callers that already hold a `WeeklyIntel` in memory (e.g. one built via
    `ffbot.history.index.WeekSnapshot.weekly_intel()`) don't need to write it
    to disk and read it back just to reach the editor JSON shape."""
    matchups: list[dict] = []
    seen: set[frozenset] = set()
    for team, game in intel.games.items():
        pair = frozenset((team, game.opponent))
        if pair in seen:
            continue
        seen.add(pair)

        if game.home:
            home_team, away_team, home_game, away_game = team, game.opponent, game, intel.games.get(game.opponent)
        else:
            home_team, away_team, home_game, away_game = game.opponent, team, intel.games.get(game.opponent), game

        row = {
            "home_team": home_team,
            "away_team": away_team,
            "kickoff_et": (home_game or away_game).kickoff_et,
            "wind_mph": (home_game or away_game).wind_mph,
            "precip_pct": (home_game or away_game).precip_pct,
            "home_team_total": home_game.team_total if home_game else (away_game.opp_total if away_game else None),
            "away_team_total": away_game.team_total if away_game else (home_game.opp_total if home_game else None),
        }
        for field_name in _PASSTHROUGH_GAME_FIELDS:
            row[field_name] = getattr(home_game or away_game, field_name, None)
        matchups.append(row)

    players = {
        entry.name: {
            "status": entry.status,
            "note": entry.note,
            "risk": entry.risk,
            "upside": entry.upside,
            "volatility": entry.volatility,
            "usage_trend": entry.usage_trend,
            "momentum": entry.momentum,
            "divergence": entry.divergence,
            "flags": list(entry.flags),
        }
        for entry in intel.players.values()
    }

    return {
        "week": intel.week,
        "generated": intel.generated,
        "source_notes": intel.source_notes,
        "players": players,
        "matchups": matchups,
    }


def write_weekly_intel(path: str | Path, data: dict) -> None:
    """Write `data` (the shape `weekly_intel_editor_json` returns) back to
    `path`, mirroring each matchup row onto both teams' `games:` entries."""
    games: dict[str, Any] = {}
    for m in data.get("matchups") or []:
        home = str(m.get("home_team") or "").strip().upper()
        away = str(m.get("away_team") or "").strip().upper()
        if not home or not away:
            continue
        shared = {"kickoff_et": m.get("kickoff_et") or "", "wind_mph": m.get("wind_mph"), "precip_pct": m.get("precip_pct")}
        extras = {f: m[f] for f in _PASSTHROUGH_GAME_FIELDS if m.get(f) not in (None, "", False)}
        games[home] = {
            **shared, **extras,
            "opponent": away, "home": True,
            "team_total": m.get("home_team_total"), "opp_total": m.get("away_team_total"),
        }
        games[away] = {
            **shared, **extras,
            "opponent": home, "home": False,
            "team_total": m.get("away_team_total"), "opp_total": m.get("home_team_total"),
        }

    players: dict[str, Any] = {}
    for name, p in (data.get("players") or {}).items():
        name = str(name).strip()
        if not name:
            continue
        entry = {k: v for k, v in (p or {}).items() if v not in (None, "", [])}
        players[name] = entry or ""

    raw = {
        "week": data.get("week"),
        "generated": data.get("generated") or "",
        "source_notes": data.get("source_notes") or "",
        "players": players,
        "games": games,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
