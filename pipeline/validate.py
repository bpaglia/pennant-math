#!/usr/bin/env python3
"""Validate raw data in data/raw/ before any metric is computed.

Usage:
    python pipeline/validate.py                 # check data/raw
    python pipeline/validate.py --raw some/dir
    python pipeline/validate.py --strict        # treat warnings as failures

Exit code 0 means every check passed (warnings are allowed unless --strict).
Exit code 1 means at least one error, so a scheduled build should stop and
nothing bad is published.

ERROR: the data is impossible or internally inconsistent.
WARN:  unusual, but it can happen for real (for example a cancelled game that
       was never replayed), so compare against official standings.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KNOWN_STATES = {
    "scheduled", "in_progress", "final", "postponed", "canceled",
    "delayed", "suspended", "abandoned", "unknown",
}
OPEN_STATES = {"scheduled", "in_progress", "delayed", "suspended"}
SEASON_TYPES = {"regular", "postseason"}

LEAGUES = {"American", "National"}
TEAMS_PER_DIVISION = 5
DIVISIONS_PER_LEAGUE = 3
TEAM_COUNT = 30

DEFAULT_GAMES_PER_TEAM = 162
# The 2020 season was shortened to 60 games per team (898 games in total).
GAMES_PER_TEAM_BY_SEASON = {2020: 60}
TOTAL_GAMES_BY_SEASON = {2020: 898}

MIN_AVG_RUNS_PER_TEAM_GAME = 3.0
MAX_AVG_RUNS_PER_TEAM_GAME = 6.5
MIN_GAMES_FOR_AVERAGE_CHECK = 100
SUSPICIOUS_RUNS = 30
STALE_DAYS = 2


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def check_teams(teams: list, report: Report) -> set:
    """Check the 30-team structure and return the set of team ids."""
    if len(teams) != TEAM_COUNT:
        report.error(f"Expected {TEAM_COUNT} teams, found {len(teams)}")

    ids = [team.get("id") for team in teams]
    abbreviations = [team.get("abbreviation") for team in teams]
    for label, values in (("id", ids), ("abbreviation", abbreviations)):
        for value, count in Counter(values).items():
            if count > 1:
                report.error(f"Duplicate team {label}: {value}")

    by_league = Counter(team.get("league") for team in teams)
    for league in LEAGUES:
        if by_league.get(league, 0) != TEAM_COUNT // 2:
            report.error(f"{league} League has {by_league.get(league, 0)} teams, expected {TEAM_COUNT // 2}")
    for league in by_league:
        if league not in LEAGUES:
            report.error(f"Unexpected league name: {league!r}")

    by_division = Counter((team.get("league"), team.get("division")) for team in teams)
    if len(by_division) != len(LEAGUES) * DIVISIONS_PER_LEAGUE:
        report.error(f"Expected {len(LEAGUES) * DIVISIONS_PER_LEAGUE} divisions, found {len(by_division)}")
    for division, count in by_division.items():
        if count != TEAMS_PER_DIVISION:
            report.error(f"Division {division} has {count} teams, expected {TEAMS_PER_DIVISION}")

    return {team_id for team_id in ids if team_id is not None}


def check_games(games: list, team_ids: set, today: date, report: Report) -> None:
    """Game-level checks that apply to every season."""
    ids = Counter(game.get("id") for game in games)
    for game_id, count in ids.items():
        if count > 1:
            report.error(f"Game id {game_id} appears {count} times")

    for game in games:
        gid = game.get("id")
        home, away = game.get("home_team_id"), game.get("away_team_id")
        state = game.get("status_state")

        if gid is None or game.get("date_et") is None:
            report.error(f"Game missing id or date: {game}")
            continue
        if home not in team_ids or away not in team_ids:
            report.error(f"Game {gid}: unknown team id (home={home}, away={away})")
        if home == away:
            report.error(f"Game {gid}: a team is playing itself ({home})")
        if game.get("season_type") not in SEASON_TYPES:
            report.error(f"Game {gid}: unexpected season_type {game.get('season_type')!r}")
        if state not in KNOWN_STATES:
            report.error(f"Game {gid}: unknown status_state {state!r}")

        game_day = date.fromisoformat(game["date_et"])
        if state == "final":
            runs = (game.get("home_runs"), game.get("away_runs"))
            if any(not isinstance(value, int) or value < 0 for value in runs):
                report.error(f"Game {gid}: final game with invalid runs {runs}")
            elif runs[0] == runs[1]:
                report.error(f"Game {gid}: final game ended tied {runs[0]}-{runs[1]}")
            elif max(runs) > SUSPICIOUS_RUNS:
                report.warn(f"Game {gid}: unusually high score {runs[0]}-{runs[1]}; verify")
            if game_day > today + timedelta(days=1):
                report.error(f"Game {gid}: marked final but dated in the future ({game['date_et']})")
        elif state in OPEN_STATES and game_day < today - timedelta(days=STALE_DAYS):
            report.warn(f"Game {gid}: still '{state}' but dated {game['date_et']}; status may be stale")


def check_season(season: int, games: list, report: Report) -> None:
    """Season-level checks on the regular season."""
    regular = [game for game in games if game.get("season_type") == "regular"]
    if not regular:
        report.warn(f"{season}: no regular-season games found")
        return

    finals = [game for game in regular if game.get("status_state") == "final"]
    per_team = Counter()
    for game in finals:
        per_team[game["home_team_id"]] += 1
        per_team[game["away_team_id"]] += 1

    games_per_team = GAMES_PER_TEAM_BY_SEASON.get(season, DEFAULT_GAMES_PER_TEAM)
    expected_total = TOTAL_GAMES_BY_SEASON.get(season, TEAM_COUNT * games_per_team // 2)

    for team_id, count in per_team.items():
        if count > games_per_team:
            report.error(f"{season}: team {team_id} has {count} final games, more than {games_per_team}")

    if len(finals) > expected_total:
        report.error(f"{season}: {len(finals)} final games, more than the {expected_total} possible")

    season_complete = not any(game.get("status_state") in OPEN_STATES for game in regular)
    if season_complete:
        if len(finals) < expected_total:
            report.warn(
                f"{season}: season looks complete but has {len(finals)} final games "
                f"(expected {expected_total}); some games may be missing or were cancelled. "
                "Compare against official standings."
            )
        short = sorted(team_id for team_id, count in per_team.items() if count < games_per_team)
        if short:
            report.warn(f"{season}: {len(short)} teams have fewer than {games_per_team} games: {short}")

    if len(finals) >= MIN_GAMES_FOR_AVERAGE_CHECK:
        total_runs = sum(game["home_runs"] + game["away_runs"] for game in finals)
        average = total_runs / (2 * len(finals))
        if not MIN_AVG_RUNS_PER_TEAM_GAME <= average <= MAX_AVG_RUNS_PER_TEAM_GAME:
            report.error(
                f"{season}: average of {average:.2f} runs per team per game is implausible "
                f"(expected {MIN_AVG_RUNS_PER_TEAM_GAME} to {MAX_AVG_RUNS_PER_TEAM_GAME})"
            )


def load_raw(raw_dir: Path):
    teams_path = raw_dir / "teams.json"
    if not teams_path.exists():
        raise FileNotFoundError(f"{teams_path} not found. Run pipeline/fetch.py first.")
    teams = json.loads(teams_path.read_text(encoding="utf-8")).get("teams", [])

    games_by_season = defaultdict(list)
    game_files = sorted(raw_dir.glob("games_*.json"))
    if not game_files:
        raise FileNotFoundError(f"No games_*.json files in {raw_dir}. Run pipeline/fetch.py first.")
    for path in game_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for game in payload.get("games", []):
            games_by_season[game.get("season")].append(game)
    return teams, games_by_season


def validate(teams: list, games_by_season: dict, today: date) -> Report:
    report = Report()
    team_ids = check_teams(teams, report)
    all_games = [game for games in games_by_season.values() for game in games]
    check_games(all_games, team_ids, today, report)
    for season in sorted(games_by_season):
        check_season(season, games_by_season[season], report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    args = parser.parse_args(argv)

    try:
        teams, games_by_season = load_raw(args.raw)
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    report = validate(teams, games_by_season, today)

    for message in report.warnings:
        print(f"WARN   {message}")
    for message in report.errors:
        print(f"ERROR  {message}")

    seasons = ", ".join(str(season) for season in sorted(games_by_season))
    total = sum(len(games) for games in games_by_season.values())
    print(f"checked {len(teams)} teams and {total} games ({seasons}): "
          f"{len(report.errors)} errors, {len(report.warnings)} warnings")

    if report.errors or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
