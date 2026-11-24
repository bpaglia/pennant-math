#!/usr/bin/env python3
"""Scoreboard archive: every game day of a season, with tags and the playoff race after that day.

Usage:
    python pipeline/archive.py                       # every season in the data
    python pipeline/archive.py --seasons 2026        # just one (what the daily run does)
    python pipeline/archive.py --rebuild             # recompute days already saved

For each day with final games it saves the games (score, innings, the winner's chance beforehand,
tags) and each team's playoff and division odds after that day, as of the Elo ratings that day.
Days already in a season's file are skipped, so a daily run only adds the new day. Odds use fewer
simulations than the daily recap, so day-to-day changes carry about a point of noise.

Writes data/processed/archive_<season>.json. Needs numpy and the files from compute.py.
Exit code 0: success. 2: inputs missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from compute import ELO_DEFAULTS, expected_home_win, league_final_games, utc_now_iso, write_json
from odds import DEFAULT_RATING_NOISE, SeasonState, ratings_as_of, simulate
from recap import game_tags
from validate import load_raw

DEFAULT_SIMS = 3000


def permille(values) -> list:
    return [int(round(float(v) * 1000)) for v in values]


def build_season(teams: list, season_games: list, elo_data: dict, season: int, n_sims: int, seed: int,
                 home_advantage: float, rating_noise: float, existing: dict = None):
    """The season's archive, adding any days not already saved. Returns (archive, days_added)."""
    ids = sorted(team["id"] for team in teams)
    by_day = defaultdict(list)
    for game in league_final_games(season_games, set(ids)):
        by_day[game["date_et"]].append(game)
    if not by_day:
        return existing, 0
    archive = existing or {"meta": {"season": season, "ids": ids}, "days": {}}
    archive["meta"].update(sims=n_sims, generated_at_utc=utc_now_iso())

    def odds_as_of(as_of):
        state = SeasonState(teams, season_games, as_of)
        return simulate(state, ratings_as_of(elo_data, season, as_of, state.ids), n_sims, seed,
                        home_advantage, rating_noise)

    if "pre" not in archive["meta"]:
        first = min(by_day)
        archive["meta"]["pre"] = permille(odds_as_of((date.fromisoformat(first) - timedelta(days=1)).isoformat())["p_playoffs"])

    added = 0
    for day in sorted(by_day):
        if day in archive["days"]:
            continue
        previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        before = ratings_as_of(elo_data, season, previous, ids)
        rows = []
        for game in by_day[day]:
            p_home = expected_home_win(before[game["home_team_id"]], before[game["away_team_id"]], home_advantage)
            p_winner = p_home if game["home_runs"] > game["away_runs"] else 1 - p_home
            rows.append([game["away_team_id"], game["home_team_id"], game["away_runs"], game["home_runs"],
                         game.get("innings"), int(round(p_winner * 1000)), game_tags(game, p_winner)])
        after = odds_as_of(day)
        archive["days"][day] = {"g": rows, "po": permille(after["p_playoffs"]), "dv": permille(after["p_division"])}
        added += 1
    return archive, added


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--seasons", nargs="+", type=int, default=None)
    parser.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    parser.add_argument("--seed", type=int, default=12345)
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    try:
        teams, games_by_season = load_raw(args.raw)
        elo_data = json.loads((args.processed / "elo.json").read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        print(f"{err}. Run pipeline/compute.py first.", file=sys.stderr)
        return 2

    teams = sorted(teams, key=lambda team: team["id"])
    home_advantage = elo_data.get("meta", {}).get("params", {}).get("home_advantage", ELO_DEFAULTS["home_advantage"])
    for season in args.seasons or sorted(games_by_season):
        if season not in games_by_season or str(season) not in elo_data["seasons"]:
            print(f"{season}: not in the data, skipped")
            continue
        path = args.processed / f"archive_{season}.json"
        existing = None if args.rebuild or not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        archive, added = build_season(teams, games_by_season[season], elo_data, season, args.sims, args.seed,
                                      home_advantage, DEFAULT_RATING_NOISE, existing)
        if archive is None:
            print(f"{season}: no games played yet")
            continue
        write_json(path, archive, compact=True)
        print(f"{season}: added {added} days ({len(archive['days'])} in total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
