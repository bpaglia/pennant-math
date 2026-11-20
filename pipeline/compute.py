#!/usr/bin/env python3
"""Compute standings, expected records, luck, rolling run differential, and Elo.

Usage:
    python pipeline/compute.py
    python pipeline/compute.py --raw data/raw --out data/processed
    python pipeline/compute.py --k 4 --home-advantage 24 --carry 0.667

Reads the raw files in data/raw/ and writes to data/processed/:
    standings.json    per season and team: record, run differential, Pythagenpat
                      expected record, luck, one-run / extra-inning / home / road
                      records, division rank, games remaining
    rolling_rd.json   30-game rolling run differential after each game
    elo.json          Elo ratings: start and end of each season, per-game history,
                      and diagnostics (log loss, Brier score) for tuning

Only games between two MLB teams count. Standings use the regular season;
Elo also learns from postseason games. Run pipeline/validate.py first.

Exit code 0: success. 1: an integrity check failed or a computed record does not
match a verified official record. 2: input files are missing.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from validate import OPEN_STATES, is_league_game, load_raw

PYTHAGENPAT_EXPONENT = 0.287
ROLLING_WINDOW = 30
ELO_DEFAULTS = {
    "initial": 1500.0,        # every team's rating before the first replayed game
    "k": 4.0,                 # how far one game moves a rating
    "home_advantage": 24.0,   # rating points added to the home team for prediction
    "carry": 2 / 3,           # share of last season's deviation from 1500 kept in the offseason
}


class ComputeError(RuntimeError):
    """Raised when an integrity check fails."""


# --------------------------------------------------------------------------
# Selecting games
# --------------------------------------------------------------------------

def league_final_games(games: list, team_ids: set) -> list:
    """Final games between two MLB teams with valid scores, in date order."""
    selected = []
    for game in games:
        if game.get("status_state") != "final" or not is_league_game(game, team_ids):
            continue
        if not isinstance(game.get("home_runs"), int) or not isinstance(game.get("away_runs"), int):
            continue
        selected.append(game)
    return sorted(selected, key=lambda game: (game["date_utc"], game["id"]))


# --------------------------------------------------------------------------
# Standings, Pythagenpat, luck
# --------------------------------------------------------------------------

def pythagenpat_win_pct(runs_scored: int, runs_allowed: int, games: int) -> float:
    """Expected win percentage with an exponent that adapts to the run environment."""
    if runs_scored == 0 and runs_allowed == 0:
        return 0.5
    exponent = ((runs_scored + runs_allowed) / games) ** PYTHAGENPAT_EXPONENT
    scored, allowed = runs_scored ** exponent, runs_allowed ** exponent
    return scored / (scored + allowed)


def _blank_stats() -> dict:
    return {
        "wins": 0, "losses": 0, "rs": 0, "ra": 0,
        "one_run_w": 0, "one_run_l": 0, "extra_w": 0, "extra_l": 0,
        "home_w": 0, "home_l": 0, "road_w": 0, "road_l": 0, "remaining": 0,
        "home_rs": 0, "home_ra": 0, "road_rs": 0, "road_ra": 0,
    }


def compute_standings(season_games: list, teams: list) -> dict:
    """Regular-season standings for one season. Returns {"teams": [...], ...}."""
    team_ids = {team["id"] for team in teams}
    stats = {team["id"]: _blank_stats() for team in teams}
    last_date = None

    for game in season_games:
        if game.get("season_type") != "regular" or not is_league_game(game, team_ids):
            continue
        home, away = game["home_team_id"], game["away_team_id"]
        state = game.get("status_state")

        if state in OPEN_STATES:
            stats[home]["remaining"] += 1
            stats[away]["remaining"] += 1
            continue
        if state != "final":
            continue
        home_runs, away_runs = game.get("home_runs"), game.get("away_runs")
        if not isinstance(home_runs, int) or not isinstance(away_runs, int):
            continue

        last_date = max(last_date or game["date_et"], game["date_et"])
        margin = abs(home_runs - away_runs)
        extra_innings = (game.get("innings") or 0) > 9
        for team_id, scored, allowed, at_home in (
            (home, home_runs, away_runs, True),
            (away, away_runs, home_runs, False),
        ):
            row = stats[team_id]
            won = scored > allowed
            row["rs"] += scored
            row["ra"] += allowed
            row["wins" if won else "losses"] += 1
            where = "home" if at_home else "road"
            row[f"{where}_rs"] += scored
            row[f"{where}_ra"] += allowed
            row[f"{where}_{'w' if won else 'l'}"] += 1
            if margin == 1:
                row["one_run_w" if won else "one_run_l"] += 1
            if extra_innings:
                row["extra_w" if won else "extra_l"] += 1

    rows = []
    for team in teams:
        s = stats[team["id"]]
        games = s["wins"] + s["losses"]
        run_diff = s["rs"] - s["ra"]
        close = s["one_run_w"] + s["one_run_l"]
        row = {
            "team_id": team["id"],
            "abbreviation": team["abbreviation"],
            "name": team["display_name"],
            "league": team["league"],
            "division": team["division"],
            "games": games,
            "wins": s["wins"],
            "losses": s["losses"],
            "win_pct": round(s["wins"] / games, 4) if games else None,
            "runs_scored": s["rs"],
            "runs_allowed": s["ra"],
            "run_diff": run_diff,
            "run_diff_per_game": round(run_diff / games, 3) if games else None,
            "pyth_win_pct": None,
            "pyth_wins": None,
            "luck": None,
            "one_run_wins": s["one_run_w"], "one_run_losses": s["one_run_l"],
            "extra_wins": s["extra_w"], "extra_losses": s["extra_l"],
            "home_wins": s["home_w"], "home_losses": s["home_l"],
            "road_wins": s["road_w"], "road_losses": s["road_l"],
            "home_run_diff": s["home_rs"] - s["home_ra"], "road_run_diff": s["road_rs"] - s["road_ra"],
            "one_run_z": round((s["one_run_w"] - close / 2) / math.sqrt(close / 4), 2) if close else None,
            "games_remaining": s["remaining"],
        }
        if games:
            pyth = pythagenpat_win_pct(s["rs"], s["ra"], games)
            row["pyth_win_pct"] = round(pyth, 4)
            row["pyth_wins"] = round(pyth * games, 2)
            row["luck"] = round(s["wins"] - pyth * games, 2)
        rows.append(row)

    _add_record_vs_winning(rows, season_games, team_ids)
    _add_division_ranks(rows)
    total_remaining = sum(row["games_remaining"] for row in rows)
    return {
        "teams": rows,
        "as_of": last_date,
        "complete": bool(last_date) and total_remaining == 0,
    }


def _add_record_vs_winning(rows: list, season_games: list, team_ids: set) -> None:
    """Record against opponents whose winning percentage is above .500 (as of the latest game)."""
    winners = {row["team_id"] for row in rows if row["win_pct"] and row["win_pct"] > 0.5}
    record = {team_id: [0, 0] for team_id in team_ids}
    for game in season_games:
        if game.get("season_type") != "regular" or game.get("status_state") != "final" or not is_league_game(game, team_ids):
            continue
        home_runs, away_runs = game.get("home_runs"), game.get("away_runs")
        if not isinstance(home_runs, int) or not isinstance(away_runs, int):
            continue
        for me, opponent, won in ((game["home_team_id"], game["away_team_id"], home_runs > away_runs),
                                  (game["away_team_id"], game["home_team_id"], away_runs > home_runs)):
            if opponent in winners:
                record[me][0 if won else 1] += 1
    for row in rows:
        row["vs_winning_wins"], row["vs_winning_losses"] = record[row["team_id"]]


def add_sos_played(standings: dict, season_games: list, end_ratings: dict, team_ids: set) -> None:
    """Average strength of the opponents already faced, relative to the league average.

    Opponent strength uses each team's latest rating, so it describes how tough the
    opponents are now. Positive means a harder schedule so far.
    """
    mean = sum(end_ratings.values()) / len(end_ratings)
    total, count = defaultdict(float), defaultdict(int)
    for game in league_final_games(season_games, team_ids):
        if game.get("season_type") != "regular":
            continue
        home, away = game["home_team_id"], game["away_team_id"]
        total[home] += end_ratings[str(away)]
        total[away] += end_ratings[str(home)]
        count[home] += 1
        count[away] += 1
    for row in standings["teams"]:
        n = count[row["team_id"]]
        row["sos_played"] = round(total[row["team_id"]] / n - mean, 1) if n else None


def _add_division_ranks(rows: list) -> None:
    """Division rank and games back. Ties are broken by run differential for display only."""
    by_division = defaultdict(list)
    for row in rows:
        by_division[(row["league"], row["division"])].append(row)
    for members in by_division.values():
        members.sort(key=lambda row: (-(row["win_pct"] or 0), -row["run_diff"], row["team_id"]))
        leader = members[0]
        for rank, row in enumerate(members, start=1):
            row["division_rank"] = rank
            row["games_back"] = ((leader["wins"] - row["wins"]) + (row["losses"] - leader["losses"])) / 2


def check_integrity(season: int, rows: list) -> None:
    """Identities that must hold. A failure means a bug, so stop the build."""
    for row in rows:
        if row["wins"] + row["losses"] != row["games"]:
            raise ComputeError(f"{season}: team {row['team_id']} wins plus losses differ from games")
    if sum(row["wins"] for row in rows) != sum(row["losses"] for row in rows):
        raise ComputeError(f"{season}: league wins do not equal league losses")
    if sum(row["runs_scored"] for row in rows) != sum(row["runs_allowed"] for row in rows):
        raise ComputeError(f"{season}: league runs scored do not equal runs allowed")


# --------------------------------------------------------------------------
# Rolling run differential
# --------------------------------------------------------------------------

def compute_rolling(regular_finals: list, window: int = ROLLING_WINDOW) -> dict:
    """Run differential over each team's last `window` games, after every game."""
    recent = defaultdict(lambda: deque(maxlen=window))
    played = Counter()
    output = defaultdict(list)
    for game in regular_finals:
        margin = game["home_runs"] - game["away_runs"]
        for team_id, team_margin in ((game["home_team_id"], margin), (game["away_team_id"], -margin)):
            recent[team_id].append(team_margin)
            played[team_id] += 1
            output[team_id].append([played[team_id], game["date_et"], sum(recent[team_id])])
    return {str(team_id): entries for team_id, entries in output.items()}


# --------------------------------------------------------------------------
# Elo
# --------------------------------------------------------------------------

def expected_home_win(home_rating: float, away_rating: float, home_advantage: float) -> float:
    difference = home_rating + home_advantage - away_rating
    return 1.0 / (1.0 + 10.0 ** (-difference / 400.0))


def margin_multiplier(margin: int) -> float:
    return math.log(abs(margin) + 1)


def run_elo(games_by_season: dict, team_ids: set, params: dict = None) -> dict:
    """Replay every game in date order and return ratings, history, and diagnostics."""
    p = dict(ELO_DEFAULTS)
    p.update(params or {})
    ratings = {team_id: p["initial"] for team_id in team_ids}
    seasons_out, diagnostics = {}, {}

    for index, season in enumerate(sorted(games_by_season)):
        if index > 0:  # offseason: pull every team back toward the average
            ratings = {tid: p["initial"] + (r - p["initial"]) * p["carry"] for tid, r in ratings.items()}
        start = dict(ratings)
        history = {team_id: [] for team_id in team_ids}
        predictions = []   # (probability home wins, 1 if home won) for regular-season games

        for game in league_final_games(games_by_season[season], team_ids):
            home, away = game["home_team_id"], game["away_team_id"]
            home_won = 1 if game["home_runs"] > game["away_runs"] else 0
            probability = expected_home_win(ratings[home], ratings[away], p["home_advantage"])
            if game.get("season_type") == "regular":
                predictions.append((probability, home_won))
            change = p["k"] * margin_multiplier(game["home_runs"] - game["away_runs"]) * (home_won - probability)
            ratings[home] += change
            ratings[away] -= change
            history[home].append([game["date_et"], round(ratings[home], 1)])
            history[away].append([game["date_et"], round(ratings[away], 1)])

        seasons_out[str(season)] = {
            "start": {str(t): round(r, 1) for t, r in start.items()},
            "end": {str(t): round(r, 1) for t, r in ratings.items()},
            "history": {str(t): entries for t, entries in history.items() if entries},
        }
        diagnostics[str(season)] = _elo_diagnostics(predictions)

    return {"params": p, "seasons": seasons_out, "diagnostics": diagnostics}


def _elo_diagnostics(predictions: list) -> dict:
    """How well the ratings predicted each regular-season game before it was played."""
    if not predictions:
        return {"games": 0}
    count = len(predictions)
    eps = 1e-9
    log_loss = -sum(
        math.log(min(max(p if won else 1 - p, eps), 1 - eps)) for p, won in predictions
    ) / count
    brier = sum((p - won) ** 2 for p, won in predictions) / count
    accuracy = sum(1 for p, won in predictions if (p >= 0.5) == bool(won)) / count
    home_rate = sum(won for _, won in predictions) / count
    baseline = (
        -(home_rate * math.log(home_rate) + (1 - home_rate) * math.log(1 - home_rate))
        if 0 < home_rate < 1 else 0.0
    )
    return {
        "games": count,
        "log_loss": round(log_loss, 4),
        "brier": round(brier, 4),
        "accuracy": round(accuracy, 4),
        "home_win_rate": round(home_rate, 4),
        "baseline_log_loss": round(baseline, 4),
    }


# --------------------------------------------------------------------------
# Verified official records
# --------------------------------------------------------------------------

def load_official_records(path: Path) -> list:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("official_records", [])


def check_official_records(standings_by_season: dict, records: list):
    """Compare computed records with verified official ones. Returns (all_ok, lines)."""
    lines, all_ok = [], True
    for record in records:
        season, team_id = str(record["season"]), record["team_id"]
        season_data = standings_by_season.get(season)
        if season_data is None:
            lines.append(f"SKIP   {season} team {team_id}: season not in the data")
            continue
        row = next((r for r in season_data["teams"] if r["team_id"] == team_id), None)
        if row is None:
            lines.append(f"FAIL   {season} team {team_id}: team not found")
            all_ok = False
        elif (row["wins"], row["losses"]) == (record["wins"], record["losses"]):
            lines.append(f"OK     {season} {row['abbreviation']}: {row['wins']}-{row['losses']} matches the official record")
        else:
            lines.append(
                f"FAIL   {season} {row['abbreviation']}: computed {row['wins']}-{row['losses']}, "
                f"official {record['wins']}-{record['losses']}"
            )
            all_ok = False
    return all_ok, lines


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_json(path: Path, payload, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    if compact:
        text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    else:
        text = json.dumps(payload, indent=1, sort_keys=True)
    temp_path.write_text(text + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def print_summary(standings_by_season: dict, elo: dict, teams_by_id: dict) -> None:
    for season in sorted(standings_by_season):
        data = standings_by_season[season]
        rows = data["teams"]
        status = "complete" if data["complete"] else "in progress"
        print(f"{season}: {sum(r['wins'] for r in rows)} games ({status}, as of {data['as_of']})")
    latest = max(standings_by_season)
    rows = [r for r in standings_by_season[latest]["teams"] if r["games"]]
    if not rows:
        return
    by_luck = sorted(rows, key=lambda r: r["luck"])
    def fmt(r):
        return f"{r['abbreviation']} {r['luck']:+.1f}"
    print(f"{latest} luck (actual minus expected wins): luckiest "
          f"{', '.join(fmt(r) for r in by_luck[::-1][:3])}; unluckiest "
          f"{', '.join(fmt(r) for r in by_luck[:3])}")
    end_ratings = elo["seasons"].get(latest, {}).get("end", {})
    top = sorted(end_ratings.items(), key=lambda item: -item[1])[:5]
    print(f"{latest} Elo, top 5 at season end: "
          + ", ".join(f"{teams_by_id[int(t)]['abbreviation']} {r:.0f}" for t, r in top))
    for season, diag in sorted(elo["diagnostics"].items()):
        if diag.get("games"):
            print(f"Elo {season}: log loss {diag['log_loss']} vs {diag['baseline_log_loss']} baseline, "
                  f"accuracy {diag['accuracy']:.1%}")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--out", type=Path, default=Path("data/processed"))
    parser.add_argument("--known", type=Path, default=None,
                        help="Verified facts file (default: known_cancellations.json next to the raw folder).")
    parser.add_argument("--k", type=float, default=ELO_DEFAULTS["k"], help="Elo K factor.")
    parser.add_argument("--home-advantage", type=float, default=ELO_DEFAULTS["home_advantage"],
                        help="Elo home-field points.")
    parser.add_argument("--carry", type=float, default=ELO_DEFAULTS["carry"],
                        help="Share of last season's rating deviation kept in the offseason.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        teams, games_by_season = load_raw(args.raw)
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 2

    teams = sorted(teams, key=lambda team: team["id"])
    team_ids = {team["id"] for team in teams}
    teams_by_id = {team["id"]: team for team in teams}
    generated = utc_now_iso()

    standings_by_season, rolling_by_season = {}, {}
    try:
        for season in sorted(games_by_season):
            season_games = games_by_season[season]
            standings = compute_standings(season_games, teams)
            check_integrity(season, standings["teams"])
            standings_by_season[str(season)] = standings
            regular_finals = [g for g in league_final_games(season_games, team_ids)
                              if g.get("season_type") == "regular"]
            rolling_by_season[str(season)] = compute_rolling(regular_finals)
    except ComputeError as err:
        print(f"Integrity check failed: {err}", file=sys.stderr)
        return 1

    elo = run_elo(games_by_season, team_ids, {
        "k": args.k, "home_advantage": args.home_advantage, "carry": args.carry,
    })

    for season, standings in standings_by_season.items():
        add_sos_played(standings, games_by_season[int(season)], elo["seasons"][season]["end"], team_ids)

    meta = {"generated_at_utc": generated, "source": "balldontlie",
            "pythagenpat_exponent": PYTHAGENPAT_EXPONENT}
    write_json(args.out / "standings.json", {"meta": meta, "seasons": standings_by_season})
    write_json(args.out / "rolling_rd.json",
               {"meta": dict(meta, window=ROLLING_WINDOW), "seasons": rolling_by_season}, compact=True)
    write_json(args.out / "elo.json", {"meta": dict(meta, params=elo["params"]),
                                       "seasons": elo["seasons"], "diagnostics": elo["diagnostics"]},
               compact=True)

    print_summary(standings_by_season, elo, teams_by_id)

    known_path = args.known or args.raw.parent / "known_cancellations.json"
    records = load_official_records(known_path)
    all_ok, lines = check_official_records(standings_by_season, records)
    for line in lines:
        print(line)
    if not all_ok:
        print("A computed record does not match a verified official record.", file=sys.stderr)
        return 1
    print(f"wrote standings.json, rolling_rd.json, elo.json to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
