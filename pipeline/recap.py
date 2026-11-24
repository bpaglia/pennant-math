#!/usr/bin/env python3
"""Daily recap: what happened on the latest game day, and what it did to the playoff race.

Usage:
    python pipeline/recap.py                      # the latest day with final games
    python pipeline/recap.py --date 2026-07-04    # any past day

For each game that day it records the score, the inning-by-inning line, tags (walk-off, comeback,
upset, extra innings, one-run, blowout, shutout, postseason), and how likely the winner was to win
beforehand. It also simulates the playoff race as of the day before and as of that day, so the site
can show who gained and lost the most. Needs numpy and the files from compute.py.

Upset: the winner had less than a 40% chance by the Elo ratings the day before.
Comeback: the winner trailed after six innings. Walk-off: a home win in its final turn at bat,
taking the lead there. These match the definitions used for the season totals in compute.py.

Writes data/processed/recap.json. Exit code 0: success. 2: inputs missing or no games that day.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from compute import ELO_DEFAULTS, expected_home_win, league_final_games, utc_now_iso, write_json
from odds import DEFAULT_RATING_NOISE, SeasonState, ratings_as_of, simulate
from validate import load_raw

UPSET_BELOW = 0.40
BLOWOUT_MARGIN = 5
MOVER_MIN = 0.005       # a change smaller than half a point is noise
MOVERS_SHOWN = 5


def line_facts(game: dict) -> dict:
    """Walk-off and comeback facts from a game's inning scores. Empty if the lines are missing or wrong."""
    home, away = game.get("home_innings"), game.get("away_innings")
    if not (isinstance(home, list) and isinstance(away, list) and home and away):
        return {}
    hv, av = [x or 0 for x in home], [x or 0 for x in away]
    if sum(hv) != game["home_runs"] or sum(av) != game["away_runs"]:
        return {}
    home_won = sum(hv) > sum(av)
    facts = {"walkoff": bool(home_won and len(hv) >= len(av) and sum(hv) - hv[-1] <= sum(av))}
    if len(hv) >= 6 and len(av) >= 6:
        facts["comeback"] = bool((home_won and sum(hv[:6]) < sum(av[:6])) or (not home_won and sum(av[:6]) < sum(hv[:6])))
    return facts


def game_tags(game: dict, p_winner) -> list:
    margin = abs(game["home_runs"] - game["away_runs"])
    facts = line_facts(game)
    tags = [name for name in ("walkoff", "comeback") if facts.get(name)]
    if p_winner is not None and p_winner < UPSET_BELOW:
        tags.append("upset")
    if (game.get("innings") or 0) > 9:
        tags.append("extras")
    if margin == 1:
        tags.append("one_run")
    if margin >= BLOWOUT_MARGIN:
        tags.append("blowout")
    if min(game["home_runs"], game["away_runs"]) == 0:
        tags.append("shutout")
    if game.get("season_type") == "postseason":
        tags.append("postseason")
    return tags


def latest_game_day(games_by_season: dict, team_ids: set):
    """The latest date with a final game between two MLB teams, and its season."""
    best = (None, None)
    for season, games in games_by_season.items():
        for game in league_final_games(games, team_ids):
            if best[1] is None or game["date_et"] > best[1]:
                best = (season, game["date_et"])
    return best


def build_recap(teams: list, season_games: list, elo_data: dict, season: int, day: str,
                n_sims: int, seed: int, home_advantage: float, rating_noise: float) -> dict:
    by_id = {team["id"]: team for team in teams}
    team_ref = lambda tid: {"id": tid, "abbr": by_id[tid]["abbreviation"], "name": by_id[tid]["display_name"]}
    previous = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    before_ratings = ratings_as_of(elo_data, season, previous, sorted(by_id))

    games = []
    for game in (g for g in league_final_games(season_games, set(by_id)) if g["date_et"] == day):
        p_home = expected_home_win(before_ratings[game["home_team_id"]], before_ratings[game["away_team_id"]], home_advantage)
        home_won = game["home_runs"] > game["away_runs"]
        p_winner = p_home if home_won else 1 - p_home
        games.append({
            "id": game["id"], "season_type": game.get("season_type"),
            "away": team_ref(game["away_team_id"]), "home": team_ref(game["home_team_id"]),
            "away_runs": game["away_runs"], "home_runs": game["home_runs"], "innings": game.get("innings"),
            "away_innings": game.get("away_innings"), "home_innings": game.get("home_innings"),
            "winner": "home" if home_won else "away", "p_winner": round(p_winner, 3),
            "tags": game_tags(game, p_winner),
        })

    def race(as_of):
        state = SeasonState(teams, season_games, as_of)
        return state, simulate(state, ratings_as_of(elo_data, season, as_of, state.ids), n_sims, seed,
                               home_advantage, rating_noise)

    state_before, before = race(previous)
    state_after, after = race(day)
    odds = {}
    for i, team_id in enumerate(state_after.ids):
        odds[str(team_id)] = {
            "before": round(float(before["p_playoffs"][i]), 3), "after": round(float(after["p_playoffs"][i]), 3),
            "div_before": round(float(before["p_division"][i]), 3), "div_after": round(float(after["p_division"][i]), 3),
        }
    changes = sorted(((o["after"] - o["before"], tid) for tid, o in odds.items()), reverse=True)

    def mover(tid):
        return dict(team_ref(int(tid)), before=odds[tid]["before"], after=odds[tid]["after"])

    count = lambda tag: sum(tag in g["tags"] for g in games)
    upsets = [g for g in games if "upset" in g["tags"]]
    biggest = min(upsets, key=lambda g: g["p_winner"]) if upsets else None
    return {
        "meta": {"date": day, "previous_date": previous, "season": season, "season_complete": state_after.G == 0,
                 "n_sims": n_sims, "generated_at_utc": utc_now_iso()},
        "summary": {"games": len(games), "upsets": count("upset"), "walkoffs": count("walkoff"),
                    "comebacks": count("comeback"), "extras": count("extras"), "one_run": count("one_run"),
                    "blowouts": count("blowout"),
                    "biggest_upset": None if not biggest else {
                        "game_id": biggest["id"], "p_winner": biggest["p_winner"],
                        "winner": biggest[biggest["winner"]]["abbr"],
                        "loser": biggest["away" if biggest["winner"] == "home" else "home"]["abbr"]}},
        "games": games, "odds": odds,
        "movers": {"up": [mover(tid) for delta, tid in changes[:MOVERS_SHOWN] if delta >= MOVER_MIN],
                   "down": [mover(tid) for delta, tid in reversed(changes[-MOVERS_SHOWN:]) if delta <= -MOVER_MIN]},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--date", default=None, help="Day to recap, YYYY-MM-DD (default: the latest game day).")
    parser.add_argument("--sims", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=12345)
    args = parser.parse_args(argv)
    try:
        teams, games_by_season = load_raw(args.raw)
        elo_data = json.loads((args.processed / "elo.json").read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        print(f"{err}. Run pipeline/compute.py first.", file=sys.stderr)
        return 2

    team_ids = {team["id"] for team in teams}
    season, day = latest_game_day(games_by_season, team_ids)
    if args.date:
        season = next((s for s, games in games_by_season.items()
                       if any(g["date_et"] == args.date for g in league_final_games(games, team_ids))), None)
        day = args.date
    if season is None or day is None:
        print("No games found for that day.", file=sys.stderr)
        return 2

    home_advantage = elo_data.get("meta", {}).get("params", {}).get("home_advantage", ELO_DEFAULTS["home_advantage"])
    recap = build_recap(sorted(teams, key=lambda t: t["id"]), games_by_season[season], elo_data, season, day,
                        args.sims, args.seed, home_advantage, DEFAULT_RATING_NOISE)
    write_json(args.processed / "recap.json", recap)
    s = recap["summary"]
    print(f"Recap for {day}: {s['games']} games, {s['upsets']} upsets, {s['walkoffs']} walk-offs, {s['comebacks']} comebacks")
    for label, key in (("gained", "up"), ("lost", "down")):
        print(f"  most playoff odds {label}: " + ", ".join(
            f"{m['abbr']} {m['before']:.0%}→{m['after']:.0%}" for m in recap["movers"][key][:3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
