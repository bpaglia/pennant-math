#!/usr/bin/env python3
"""Playoff odds, projected wins, and remaining schedule strength.

Usage:
    python pipeline/odds.py                                  # latest season, as of its last game
    python pipeline/odds.py --season 2026 --as-of 2026-08-15
    python pipeline/odds.py --backtest 2024 2025 2026 --sims 2000 --step-days 7
    python pipeline/odds.py --tune-noise 0 10 20 30 40 --sims 1000 --step-days 14

How it works: as of a date, take every game already played as fixed, treat every
later regular-season game as unplayed, and simulate those games thousands of times.
Each game is decided by the Elo ratings as of that date (ratings are held fixed
within a simulation). In each simulation, each league sends six teams to the
playoffs: three division winners and three wild cards. The share of simulations in
which a team gets in is its playoff probability.

Ties in wins are broken by head-to-head record among the tied teams, then division
record, then a random draw. That is a simplification of MLB's full rules, which
the field check below tests against the teams that actually reached each postseason.

Needs numpy and the files written by compute.py (data/processed/elo.json).

Output (data/processed/):
    odds.json           team-by-team odds as of one date
    backtest.json       how well the model's odds matched reality (with --backtest)
    odds_history.json   playoff odds over each season, for charts (with --backtest)

Exit code 0: success. 1: an internal consistency check failed. 2: inputs missing.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from compute import ELO_DEFAULTS, league_final_games, utc_now_iso, write_json
from validate import OPEN_STATES, is_league_game, load_raw

DEFAULT_SIMS = 10_000
DEFAULT_SEED = 12345
DEFAULT_RATING_NOISE = 30.0    # Elo points of uncertainty in each team's true strength. Chosen with
                               # --tune-noise on the 2023-2026 backtest: lowest overall Brier score,
                               # calibration error roughly halved, and it helps 2024 and 2025 without
                               # hurting 2026. Revisit when more seasons are added.
CHUNK_SIZE = 500
DIVISION_WINNERS = 3
WILD_CARDS = 3
BYES = 2
SKIPPED_STATES = {"canceled", "postponed", "abandoned", "unknown"}


class OddsError(RuntimeError):
    """Raised when the simulation breaks an identity that must always hold."""


# --------------------------------------------------------------------------
# Season state as of a date
# --------------------------------------------------------------------------

class SeasonState:
    """Who has won what by `as_of`, and which regular-season games are still to play.

    Games on or before `as_of` that are final count as played. Every other regular-season
    game between two MLB teams is unplayed, even if the data already holds its result;
    that is what lets a past season be replayed as if it were still in progress.
    """

    def __init__(self, teams: list, season_games: list, as_of: str, include_remaining: bool = True):
        self.teams = sorted(teams, key=lambda team: team["id"])
        self.n = len(self.teams)
        self.ids = [team["id"] for team in self.teams]
        self.idx = {team_id: i for i, team_id in enumerate(self.ids)}
        self.league = [team["league"] for team in self.teams]
        self.division = [(team["league"], team["division"]) for team in self.teams]
        self.as_of = as_of

        n = self.n
        self.wins = np.zeros(n, dtype=np.int32)
        self.losses = np.zeros(n, dtype=np.int32)
        self.h2h = np.zeros((n, n), dtype=np.int32)          # played wins of i over j
        self.pair_total = np.zeros((n, n), dtype=np.int32)   # games between i and j, played plus unplayed
        id_set = set(self.ids)
        home_list, away_list = [], []

        for game in season_games:
            if game.get("season_type") != "regular" or not is_league_game(game, id_set):
                continue
            state = game.get("status_state")
            h, a = self.idx[game["home_team_id"]], self.idx[game["away_team_id"]]
            has_score = isinstance(game.get("home_runs"), int) and isinstance(game.get("away_runs"), int)
            if state == "final" and has_score and game["date_et"] <= as_of:
                winner, loser = (h, a) if game["home_runs"] > game["away_runs"] else (a, h)
                self.wins[winner] += 1
                self.losses[loser] += 1
                self.h2h[winner, loser] += 1
                self.pair_total[h, a] += 1
                self.pair_total[a, h] += 1
            elif include_remaining and state not in SKIPPED_STATES and (state in OPEN_STATES or (state == "final" and has_score)):
                home_list.append(h)
                away_list.append(a)
                self.pair_total[h, a] += 1
                self.pair_total[a, h] += 1

        self.rem_home = np.array(home_list, dtype=np.int32)
        self.rem_away = np.array(away_list, dtype=np.int32)
        self.G = len(home_list)

        # wins_added = home_won @ D + base, where home_won is a (sims x games) 0/1 matrix.
        self.D = np.zeros((self.G, n), dtype=np.float32)
        if self.G:
            rows = np.arange(self.G)
            self.D[rows, self.rem_home] = 1.0
            self.D[rows, self.rem_away] = -1.0
        self.base = np.bincount(self.rem_away, minlength=n).astype(np.float32)

        # Index unplayed games by matchup and by division, for exact tiebreaks.
        pair_home = defaultdict(list)
        for g, (h, a) in enumerate(zip(home_list, away_list)):
            pair_home[(h, a)].append(g)
        self.pair_home_cols = {pair: np.array(cols, dtype=np.int32) for pair, cols in pair_home.items()}
        self.div_home_cols = [self._cols(t, home=True) for t in range(n)]
        self.div_away_cols = [self._cols(t, home=False) for t in range(n)]
        self.div_played_w = np.zeros(n, dtype=np.int32)
        for i in range(n):
            for j in range(n):
                if i != j and self.division[i] == self.division[j]:
                    self.div_played_w[i] += self.h2h[i, j]
        self.div_total = np.array(
            [sum(self.pair_total[i, j] for j in range(n) if j != i and self.division[i] == self.division[j])
             for i in range(n)], dtype=np.int32)

        self.league_divisions = {}
        for league in sorted(set(self.league)):
            groups = defaultdict(list)
            for i in range(n):
                if self.league[i] == league:
                    groups[self.division[i]].append(i)
            self.league_divisions[league] = [groups[key] for key in sorted(groups)]

    def _cols(self, team: int, home: bool) -> np.ndarray:
        if self.G == 0:
            return np.zeros(0, dtype=np.int32)
        mine = self.rem_home if home else self.rem_away
        theirs = self.rem_away if home else self.rem_home
        same_division = np.array([self.division[o] == self.division[team] for o in range(self.n)])
        return np.nonzero((mine == team) & same_division[theirs])[0].astype(np.int32)


def ratings_as_of(elo_data: dict, season: int, as_of: str, team_ids: list) -> dict:
    """Each team's Elo rating after the last game on or before `as_of`."""
    season_elo = elo_data["seasons"][str(season)]
    ratings = {}
    for team_id in team_ids:
        rating = season_elo["start"][str(team_id)]
        history = season_elo["history"].get(str(team_id), [])
        position = bisect.bisect_right([entry[0] for entry in history], as_of)
        if position:
            rating = history[position - 1][1]
        ratings[team_id] = rating
    return ratings


# --------------------------------------------------------------------------
# Tiebreaks
# --------------------------------------------------------------------------

def _rank_exact(state: SeasonState, teams: list, wins_row, hw_row, rng) -> list:
    """Order `teams` best first: wins, then head-to-head, then division record, then chance."""

    def h2h_wins(i, j):
        total = int(state.h2h[i, j])
        cols = state.pair_home_cols.get((i, j))
        if cols is not None and len(cols):
            total += int(hw_row[cols].sum())
        cols = state.pair_home_cols.get((j, i))
        if cols is not None and len(cols):
            total += len(cols) - int(hw_row[cols].sum())
        return total

    def division_pct(t):
        won = int(state.div_played_w[t])
        home, away = state.div_home_cols[t], state.div_away_cols[t]
        won += int(hw_row[home].sum()) + len(away) - int(hw_row[away].sum())
        return won / state.div_total[t] if state.div_total[t] else 0.5

    def order_tied(group):
        if len(group) == 1:
            return list(group)
        keys = {}
        for i in group:
            won = games = 0
            for j in group:
                if i != j:
                    won += h2h_wins(i, j)
                    games += int(state.pair_total[i, j])
            keys[i] = (-(won / games if games else 0.5), -division_pct(i), rng.random())
        return sorted(group, key=keys.get)

    by_wins = sorted(teams, key=lambda t: -int(wins_row[t]))
    ordered, start = [], 0
    while start < len(by_wins):
        end = start
        while end + 1 < len(by_wins) and wins_row[by_wins[end + 1]] == wins_row[by_wins[start]]:
            end += 1
        ordered.extend(order_tied(by_wins[start:end + 1]))
        start = end + 1
    return ordered


def resolve_league_exact(state: SeasonState, league: str, wins_row, hw_row, rng):
    """Division winners, bye teams, and wild cards for one league in one simulation."""
    divisions = state.league_divisions[league]
    winners = [_rank_exact(state, division, wins_row, hw_row, rng)[0] for division in divisions]
    ranked_winners = _rank_exact(state, winners, wins_row, hw_row, rng)
    others = [t for division in divisions for t in division if t not in winners]
    wild_cards = _rank_exact(state, others, wins_row, hw_row, rng)[:WILD_CARDS]
    return winners, ranked_winners[:BYES], wild_cards


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

def home_win_probabilities(state: SeasonState, ratings: dict, home_advantage: float) -> np.ndarray:
    r = np.array([ratings[team_id] for team_id in state.ids], dtype=np.float64)
    if state.G == 0:
        return np.zeros(0)
    difference = r[state.rem_home] + home_advantage - r[state.rem_away]
    return 1.0 / (1.0 + 10.0 ** (-difference / 400.0))


def _classify(state: SeasonState, wins: np.ndarray, hw: np.ndarray, rng):
    """Who wins divisions, gets byes, and makes the playoffs in each simulation."""
    c, n = wins.shape
    rows = np.arange(c)
    key = wins + rng.random((c, n)) * 0.5      # random tiebreak; ties that matter are redone exactly
    is_div = np.zeros((c, n), dtype=bool)
    is_bye = np.zeros((c, n), dtype=bool)
    is_po = np.zeros((c, n), dtype=bool)
    needs_exact = {}

    for league, divisions in state.league_divisions.items():
        flag = np.zeros(c, dtype=bool)
        winners = []
        for division in divisions:
            members = np.array(division)
            winner = members[key[:, members].argmax(axis=1)]
            is_div[rows, winner] = True
            winners.append(winner)
            top = -np.sort(-wins[:, members], axis=1)
            flag |= top[:, 0] == top[:, 1]
        winners = np.stack(winners, axis=1)
        winner_wins = np.take_along_axis(wins, winners, axis=1)
        winner_keys = np.take_along_axis(key, winners, axis=1)
        best_first = np.argsort(-winner_keys, axis=1)
        byes = np.take_along_axis(winners, best_first[:, :BYES], axis=1)
        for column in range(BYES):
            is_bye[rows, byes[:, column]] = True
        sorted_winner_wins = -np.sort(-winner_wins, axis=1)
        flag |= sorted_winner_wins[:, BYES - 1] == sorted_winner_wins[:, BYES]

        members = np.array([t for division in divisions for t in division])
        others_wins = np.where(is_div[:, members], -1, wins[:, members])
        others_keys = np.where(is_div[:, members], -1.0, key[:, members])
        wild = members[np.argsort(-others_keys, axis=1)[:, :WILD_CARDS]]
        for column in range(WILD_CARDS):
            is_po[rows, wild[:, column]] = True
        sorted_others = -np.sort(-others_wins, axis=1)
        flag |= sorted_others[:, WILD_CARDS - 1] == sorted_others[:, WILD_CARDS]
        needs_exact[league] = flag

    is_po |= is_div

    for league, flag in needs_exact.items():
        members = [t for division in state.league_divisions[league] for t in division]
        for s in np.nonzero(flag)[0]:
            is_div[s, members] = False
            is_bye[s, members] = False
            is_po[s, members] = False
            winners, byes, wild = resolve_league_exact(state, league, wins[s], hw[s], rng)
            is_div[s, winners] = True
            is_bye[s, byes] = True
            is_po[s, winners + wild] = True
    return is_div, is_bye, is_po


def simulate(state: SeasonState, ratings: dict, n_sims: int = DEFAULT_SIMS, seed: int = DEFAULT_SEED,
             home_advantage: float = ELO_DEFAULTS["home_advantage"], rating_noise: float = DEFAULT_RATING_NOISE) -> dict:
    """Play out the rest of the season `n_sims` times.

    With `rating_noise` above zero, each simulation gives every team its own true strength,
    drawn around its Elo rating with that standard deviation (in rating points) and held
    fixed for the rest of that simulation. Elo ratings are estimates, not facts, so this
    widens the range of outcomes and keeps favorites and long shots from looking too certain.
    """
    rng = np.random.default_rng(seed)
    p_home = home_win_probabilities(state, ratings, home_advantage)
    base_ratings = np.array([ratings[team_id] for team_id in state.ids], dtype=np.float64)
    division_count = np.zeros(state.n)
    playoff_count = np.zeros(state.n)
    bye_count = np.zeros(state.n)
    final_wins = np.zeros((n_sims, state.n), dtype=np.int16)

    done = 0
    while done < n_sims:
        c = min(CHUNK_SIZE, n_sims - done)
        if rating_noise > 0 and state.G:
            team_ratings = base_ratings + rng.normal(0.0, rating_noise, (c, state.n))
            difference = team_ratings[:, state.rem_home] + home_advantage - team_ratings[:, state.rem_away]
            probabilities = 1.0 / (1.0 + 10.0 ** (-difference / 400.0))
        else:
            probabilities = p_home
        hw = rng.random((c, state.G)) < probabilities
        added = hw.astype(np.float32) @ state.D + state.base
        wins = (state.wins + np.rint(added)).astype(np.int32)
        is_div, is_bye, is_po = _classify(state, wins, hw, rng)
        division_count += is_div.sum(axis=0)
        playoff_count += is_po.sum(axis=0)
        bye_count += is_bye.sum(axis=0)
        final_wins[done:done + c] = wins
        done += c

    result = {
        "p_division": division_count / n_sims,
        "p_playoffs": playoff_count / n_sims,
        "p_bye": bye_count / n_sims,
        "wins_mean": final_wins.mean(axis=0),
        "wins_p10": np.percentile(final_wins, 10, axis=0),
        "wins_p90": np.percentile(final_wins, 90, axis=0),
        "n_sims": n_sims,
    }
    check_invariants(state, result)
    return result


def check_invariants(state: SeasonState, result: dict) -> None:
    """Probabilities must add up to the size of the field, or something is wrong."""
    leagues = len(state.league_divisions)
    expected = {
        "p_playoffs": leagues * (DIVISION_WINNERS + WILD_CARDS),
        "p_division": leagues * DIVISION_WINNERS,
        "p_bye": leagues * BYES,
    }
    for name, total in expected.items():
        if abs(float(result[name].sum()) - total) > 1e-6:
            raise OddsError(f"{name} adds up to {result[name].sum():.4f}, expected {total}")


def schedule_and_projection(state: SeasonState, ratings: dict, home_advantage: float) -> dict:
    """Expected remaining wins and remaining schedule strength for every team."""
    n = state.n
    r = np.array([ratings[team_id] for team_id in state.ids], dtype=np.float64)
    expected_wins = np.zeros(n)
    opponent_sum = np.zeros(n)
    count = np.zeros(n)
    if state.G:
        p_home = home_win_probabilities(state, ratings, home_advantage)
        np.add.at(expected_wins, state.rem_home, p_home)
        np.add.at(expected_wins, state.rem_away, 1 - p_home)
        # A home team's opponent is effectively weaker by the home edge, and vice versa.
        np.add.at(opponent_sum, state.rem_home, r[state.rem_away] - home_advantage)
        np.add.at(opponent_sum, state.rem_away, r[state.rem_home] + home_advantage)
        np.add.at(count, state.rem_home, 1)
        np.add.at(count, state.rem_away, 1)
    league_mean = r.mean()
    sos = np.where(count > 0, opponent_sum / np.maximum(count, 1) - league_mean, np.nan)
    return {"expected_wins_remaining": expected_wins, "sos_remaining": sos, "games_remaining": count.astype(int)}


def build_team_rows(state: SeasonState, ratings: dict, result: dict, extra: dict) -> list:
    rows = []
    for i, team in enumerate(state.teams):
        sos = extra["sos_remaining"][i]
        rows.append({
            "team_id": team["id"],
            "abbreviation": team["abbreviation"],
            "name": team["display_name"],
            "league": team["league"],
            "division": team["division"],
            "wins": int(state.wins[i]),
            "losses": int(state.losses[i]),
            "elo": round(float(ratings[team["id"]]), 1),
            "games_remaining": int(extra["games_remaining"][i]),
            "p_division": round(float(result["p_division"][i]), 4),
            "p_playoffs": round(float(result["p_playoffs"][i]), 4),
            "p_bye": round(float(result["p_bye"][i]), 4),
            "projected_wins": round(float(result["wins_mean"][i]), 1),
            "projected_wins_p10": float(result["wins_p10"][i]),
            "projected_wins_p90": float(result["wins_p90"][i]),
            "expected_wins_remaining": round(float(extra["expected_wins_remaining"][i]), 2),
            "sos_remaining": None if np.isnan(sos) else round(float(sos), 1),
        })
    return rows


# --------------------------------------------------------------------------
# Season helpers and the field check
# --------------------------------------------------------------------------

def regular_season_span(season_games: list, team_ids: set):
    dates = sorted(g["date_et"] for g in league_final_games(season_games, team_ids)
                   if g.get("season_type") == "regular")
    return (dates[0], dates[-1]) if dates else (None, None)


def postseason_teams(season_games: list, team_ids: set) -> set:
    """Teams that played a postseason game. Every qualifier plays at least one."""
    found = set()
    for game in season_games:
        if game.get("season_type") == "postseason" and is_league_game(game, team_ids):
            found.update((game["home_team_id"], game["away_team_id"]))
    return found


def final_field(teams: list, season_games: list, seed: int = DEFAULT_SEED) -> dict:
    """The playoff field implied by the completed regular season, using our tiebreak rules."""
    team_ids = {team["id"] for team in teams}
    _, last = regular_season_span(season_games, team_ids)
    state = SeasonState(teams, season_games, last, include_remaining=False)
    result = simulate(state, {tid: 1500.0 for tid in state.ids}, n_sims=1, seed=seed)
    return {
        "playoffs": {state.ids[i] for i in range(state.n) if result["p_playoffs"][i] > 0.5},
        "division_winners": {state.ids[i] for i in range(state.n) if result["p_division"][i] > 0.5},
        "as_of": last,
    }


def season_dates(first: str, last: str, step_days: int, start_offset_days: int = 14) -> list:
    """Backtest dates: from two weeks into the season, every `step_days`, stopping before the last day."""
    current = date.fromisoformat(first) + timedelta(days=start_offset_days)
    stop = date.fromisoformat(last)
    dates = []
    while current < stop:
        dates.append(current.isoformat())
        current += timedelta(days=step_days)
    return dates


# --------------------------------------------------------------------------
# Backtest
# --------------------------------------------------------------------------

def run_backtest(teams: list, games_by_season: dict, elo_data: dict, seasons: list,
                 n_sims: int, step_days: int, seed: int, home_advantage: float,
                 rating_noise: float = DEFAULT_RATING_NOISE) -> dict:
    """Replay past seasons date by date and score the odds against what happened."""
    team_ids = {team["id"] for team in teams}
    records = []                 # one record per (season, date, team)
    history = {}
    field_check = {}

    for season in seasons:
        season_games = games_by_season[season]
        first, last = regular_season_span(season_games, team_ids)
        if not first:
            continue
        actual_playoffs = postseason_teams(season_games, team_ids)
        field = final_field(teams, season_games, seed)
        field_check[str(season)] = {
            "model_field": sorted(field["playoffs"]),
            "actual_postseason_teams": sorted(actual_playoffs),
            "matches": field["playoffs"] == actual_playoffs,
            "differences": sorted(field["playoffs"] ^ actual_playoffs),
        }
        truth_playoffs = actual_playoffs
        truth_division = field["division_winners"]
        dates = season_dates(first, last, step_days)
        history[str(season)] = {"dates": dates, "p_playoffs": {}, "p_division": {}}
        last_date = date.fromisoformat(last)

        for as_of in dates:
            state = SeasonState(teams, season_games, as_of)
            ratings = ratings_as_of(elo_data, season, as_of, state.ids)
            result = simulate(state, ratings, n_sims, seed, home_advantage, rating_noise)
            current = simulate(SeasonState(teams, season_games, as_of, include_remaining=False),
                               ratings, 1, seed, home_advantage)
            days_left = (last_date - date.fromisoformat(as_of)).days
            for i, team_id in enumerate(state.ids):
                key = str(team_id)
                history[str(season)]["p_playoffs"].setdefault(key, []).append(round(float(result["p_playoffs"][i]), 3))
                history[str(season)]["p_division"].setdefault(key, []).append(round(float(result["p_division"][i]), 3))
                records.append({
                    "season": season, "as_of": as_of, "days_left": days_left, "team_id": team_id,
                    "p": float(result["p_playoffs"][i]),
                    "y": 1.0 if team_id in truth_playoffs else 0.0,
                    "in_position_now": float(current["p_playoffs"][i] > 0.5),
                    "p_div": float(result["p_division"][i]),
                    "y_div": 1.0 if team_id in truth_division else 0.0,
                })
    return {"records": records, "history": history, "field_check": field_check}


def tune_rating_noise(teams: list, games_by_season: dict, elo_data: dict, seasons: list, values: list,
                      n_sims: int, step_days: int, seed: int, home_advantage: float) -> list:
    """Run the backtest once per candidate noise level and compare the scores."""
    rows = []
    for sigma in values:
        outcome = run_backtest(teams, games_by_season, elo_data, seasons, n_sims, step_days, seed,
                               home_advantage, rating_noise=sigma)
        summary = summarize_backtest(outcome["records"])
        early = [h for h in summary["by_horizon"] if h["horizon"].startswith("more than")]
        rows.append({
            "rating_noise": sigma,
            "brier": summary["overall"]["brier_model"],
            "brier_early": early[0]["brier_model"] if early else None,
            "calibration_error": summary["calibration_error"],
            "by_season": {season: item["brier_model"] for season, item in summary["by_season"].items()},
        })
    return rows


def summarize_backtest(records: list) -> dict:
    """Scores: lower Brier is better. Compared with two simple baselines."""
    if not records:
        return {}
    p = np.array([r["p"] for r in records])
    y = np.array([r["y"] for r in records])
    now = np.array([r["in_position_now"] for r in records])
    p_div = np.array([r["p_div"] for r in records])
    y_div = np.array([r["y_div"] for r in records])
    days = np.array([r["days_left"] for r in records])
    prior = y.mean()

    def brier(pred, truth):
        return float(np.mean((pred - truth) ** 2))

    def block(mask):
        return {
            "n": int(mask.sum()),
            "brier_model": round(brier(p[mask], y[mask]), 4),
            "brier_in_position_now": round(brier(now[mask], y[mask]), 4),
            "brier_constant": round(brier(np.full(int(mask.sum()), prior), y[mask]), 4),
        }

    horizons = [("more than 120 days left", days > 120), ("60 to 120 days left", (days > 60) & (days <= 120)),
                ("30 to 60 days left", (days > 30) & (days <= 60)), ("30 days or fewer left", days <= 30)]
    calibration = []
    for low in np.arange(0.0, 1.0, 0.1):
        high = low + 0.1
        mask = (p >= low) & ((p < high) if high < 1.0 - 1e-9 else (p <= 1.0))
        if mask.any():
            calibration.append({
                "bin": f"{low:.0%} to {high:.0%}", "n": int(mask.sum()),
                "mean_predicted": round(float(p[mask].mean()), 3),
                "observed_rate": round(float(y[mask].mean()), 3),
            })
    calibration_error = (sum(item["n"] * abs(item["mean_predicted"] - item["observed_rate"]) for item in calibration)
                         / sum(item["n"] for item in calibration)) if calibration else None
    return {
        "calibration_error": None if calibration_error is None else round(calibration_error, 4),
        "overall": block(np.ones(len(p), dtype=bool)),
        "by_horizon": [dict(horizon=name, **block(mask)) for name, mask in horizons if mask.any()],
        "by_season": {str(s): block(np.array([r["season"] == s for r in records]))
                      for s in sorted({r["season"] for r in records})},
        "division_brier_model": round(brier(p_div, y_div), 4),
        "calibration": calibration,
    }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--season", type=int, default=None, help="Season to simulate (default: latest).")
    parser.add_argument("--as-of", default=None, help="Date YYYY-MM-DD (default: the season's last game).")
    parser.add_argument("--sims", type=int, default=DEFAULT_SIMS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--backtest", nargs="+", type=int, default=None, metavar="SEASON",
                        help="Replay these seasons and score the odds.")
    parser.add_argument("--step-days", type=int, default=7, help="Days between backtest dates.")
    parser.add_argument("--rating-noise", type=float, default=DEFAULT_RATING_NOISE,
                        help="Spread, in Elo points, of each team's true strength across simulations.")
    parser.add_argument("--tune-noise", nargs="+", type=float, default=None, metavar="POINTS",
                        help="Backtest several noise levels and print a comparison (uses --backtest seasons, "
                             "or all seasons in the data).")
    return parser.parse_args(argv)


def abbreviations(ids, teams_by_id) -> str:
    return ", ".join(sorted(teams_by_id[i]["abbreviation"] for i in ids))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        teams, games_by_season = load_raw(args.raw)
        elo_path = args.processed / "elo.json"
        if not elo_path.exists():
            raise FileNotFoundError(f"{elo_path} not found. Run pipeline/compute.py first.")
        elo_data = json.loads(elo_path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        print(err, file=sys.stderr)
        return 2

    teams = sorted(teams, key=lambda team: team["id"])
    teams_by_id = {team["id"]: team for team in teams}
    team_ids = {team["id"] for team in teams}
    home_advantage = elo_data.get("meta", {}).get("params", {}).get("home_advantage", ELO_DEFAULTS["home_advantage"])
    meta = {"generated_at_utc": utc_now_iso(), "n_sims": args.sims, "seed": args.seed,
            "home_advantage": home_advantage, "rating_noise": args.rating_noise,
            "tiebreaks": "head-to-head, then division record, then random"}

    try:
        if args.tune_noise:
            seasons = args.backtest or sorted(games_by_season)
            rows = tune_rating_noise(teams, games_by_season, elo_data, seasons, args.tune_noise,
                                     args.sims, args.step_days, args.seed, home_advantage)
            write_json(args.processed / "noise_tuning.json",
                       {"meta": dict(meta, seasons=seasons, step_days=args.step_days), "results": rows})
            print_tuning(rows)
            return 0
        if args.backtest:
            outcome = run_backtest(teams, games_by_season, elo_data, args.backtest,
                                   args.sims, args.step_days, args.seed, home_advantage, args.rating_noise)
            summary = summarize_backtest(outcome["records"])
            write_json(args.processed / "backtest.json",
                       {"meta": dict(meta, seasons=args.backtest, step_days=args.step_days),
                        "summary": summary, "field_check": outcome["field_check"]})
            write_json(args.processed / "odds_history.json",
                       {"meta": dict(meta, step_days=args.step_days), "seasons": outcome["history"]}, compact=True)
            print_backtest(summary, outcome["field_check"], teams_by_id)
            return 0

        season = args.season or max(games_by_season)
        season_games = games_by_season[season]
        first, last = regular_season_span(season_games, team_ids)
        as_of = args.as_of or last
        state = SeasonState(teams, season_games, as_of)
        ratings = ratings_as_of(elo_data, season, as_of, state.ids)
        result = simulate(state, ratings, args.sims, args.seed, home_advantage, args.rating_noise)
        extra = schedule_and_projection(state, ratings, home_advantage)
        rows = build_team_rows(state, ratings, result, extra)
        write_json(args.processed / "odds.json",
                   {"meta": dict(meta, season=season, as_of=as_of), "teams": rows})
        print_odds(season, as_of, rows, state)
        if as_of == last:
            check = final_field(teams, season_games, args.seed)
            actual = postseason_teams(season_games, team_ids)
            if not actual:
                print(f"field check {season}: no postseason games in the data to compare with")
            elif check["playoffs"] == actual:
                print(f"field check {season}: OK, the model's playoff field matches the teams that played in the postseason")
            else:
                print(f"field check {season}: WARN model field differs from the postseason teams: "
                      f"model only {abbreviations(check['playoffs'] - actual, teams_by_id) or 'none'}; "
                      f"postseason only {abbreviations(actual - check['playoffs'], teams_by_id) or 'none'}")
        return 0
    except OddsError as err:
        print(f"Consistency check failed: {err}", file=sys.stderr)
        return 1


def print_odds(season: int, as_of: str, rows: list, state: SeasonState) -> None:
    print(f"{season} playoff odds as of {as_of} ({state.G} regular-season games left)")
    for league in sorted({row["league"] for row in rows}):
        top = sorted((r for r in rows if r["league"] == league), key=lambda r: -r["p_playoffs"])[:8]
        print(f"  {league}: " + ", ".join(f"{r['abbreviation']} {r['p_playoffs']:.0%}" for r in top))


def print_tuning(rows: list) -> None:
    seasons = sorted({season for row in rows for season in row["by_season"]})
    print("rating noise tuning (lower Brier and lower calibration error are better)")
    header = "  noise | Brier | early Brier | calib. error | " + " | ".join(seasons)
    print(header)
    for row in rows:
        per_season = " | ".join(f"{row['by_season'].get(season, float('nan')):.4f}" for season in seasons)
        early = "n/a" if row["brier_early"] is None else f"{row['brier_early']:.4f}"
        print(f"  {row['rating_noise']:>5.0f} | {row['brier']:.4f} | {early} | {row['calibration_error']:.4f} | {per_season}")
    best = min(rows, key=lambda row: row["brier"])
    print(f"lowest Brier: {best['rating_noise']:.0f} points")


def print_backtest(summary: dict, field_check: dict, teams_by_id: dict) -> None:
    for season, check in sorted(field_check.items()):
        if check["matches"]:
            print(f"field check {season}: OK")
        else:
            print(f"field check {season}: WARN model and actual playoff teams differ for: "
                  + abbreviations(check["differences"], teams_by_id))
    if not summary:
        print("no backtest results")
        return
    overall = summary["overall"]
    print(f"calibration error (average gap between predicted and actual): {summary['calibration_error']}")
    print(f"Brier score, all dates ({overall['n']} team-dates): model {overall['brier_model']}, "
          f"in-position-now {overall['brier_in_position_now']}, constant {overall['brier_constant']} (lower is better)")
    for item in summary["by_horizon"]:
        print(f"  {item['horizon']}: model {item['brier_model']}, in-position-now {item['brier_in_position_now']}, "
              f"constant {item['brier_constant']}")
    for season, item in summary["by_season"].items():
        print(f"  {season}: model {item['brier_model']}, constant {item['brier_constant']}")
    print("calibration (predicted vs. actual playoff rate):")
    for item in summary["calibration"]:
        print(f"  {item['bin']}: predicted {item['mean_predicted']:.0%}, actual {item['observed_rate']:.0%} (n={item['n']})")


if __name__ == "__main__":
    sys.exit(main())
