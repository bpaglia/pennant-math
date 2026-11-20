#!/usr/bin/env python3
"""Pennant Score: one 0-100 grade per team that blends how it has played, how good it is, and what is ahead.

Usage:
    python pipeline/score.py [--processed data/processed]

Reads standings.json, elo.json and (if present) odds.json from compute.py and odds.py, and writes scores.json.

Each part is a percentile among the 30 teams (100 = best):
    Results     30%   run differential per game
    Underlying  45%   Elo rating (60%) and Pythagenpat expected win% (40%)
    Outlook     25%   playoff odds (70%) and an easier remaining schedule (30%)

Outlook exists only while a season is in progress. When it is missing, the other parts are
reweighted so the score still runs from 0 to 100. The weights are starting values, to be
tuned later against how well the score predicts the rest of a season.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from compute import utc_now_iso, write_json

WEIGHTS = {"results": 0.30, "underlying": 0.45, "outlook": 0.25}


def percentiles(values: list) -> list:
    """0-100 rank of each value (ties share their average rank). None stays None."""
    present = [i for i, v in enumerate(values) if v is not None]
    out = [None] * len(values)
    if len(present) == 1:
        out[present[0]] = 50.0
    order = sorted(present, key=lambda i: values[i])
    start = 0
    while start < len(order) and len(order) > 1:
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        share = (start + end) / 2 / (len(order) - 1) * 100
        for k in range(start, end + 1):
            out[order[k]] = share
        start = end + 1
    return out


def blend(parts: list):
    """Weighted mean of the parts that exist."""
    available = [(value, weight) for value, weight in parts if value is not None]
    if not available:
        return None
    return sum(value * weight for value, weight in available) / sum(weight for _, weight in available)


def score_season(rows: list, elo_end: dict, odds_by_team: dict = None) -> dict:
    ids = [row["team_id"] for row in rows]
    results = percentiles([row["run_diff_per_game"] for row in rows])
    elo = percentiles([elo_end.get(str(team_id)) for team_id in ids])
    pythag = percentiles([row["pyth_win_pct"] for row in rows])
    underlying = [blend([(e, 0.6), (p, 0.4)]) for e, p in zip(elo, pythag)]
    if odds_by_team is None:
        outlook = [None] * len(rows)
    else:
        playoff = percentiles([odds_by_team.get(t, {}).get("p_playoffs") for t in ids])
        ease = percentiles([None if odds_by_team.get(t, {}).get("sos_remaining") is None
                            else -odds_by_team[t]["sos_remaining"] for t in ids])
        outlook = [blend([(a, 0.7), (b, 0.3)]) for a, b in zip(playoff, ease)]

    def rounded(x):
        return None if x is None else round(x, 1)

    scores = {}
    for i, team_id in enumerate(ids):
        total = blend([(results[i], WEIGHTS["results"]), (underlying[i], WEIGHTS["underlying"]),
                       (outlook[i], WEIGHTS["outlook"])])
        scores[str(team_id)] = {"score": rounded(total), "results": rounded(results[i]),
                                "underlying": rounded(underlying[i]), "outlook": rounded(outlook[i])}
    return scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    args = parser.parse_args(argv)
    try:
        standings = json.loads((args.processed / "standings.json").read_text(encoding="utf-8"))
        elo = json.loads((args.processed / "elo.json").read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        print(f"{err}. Run pipeline/compute.py first.", file=sys.stderr)
        return 2
    odds_path = args.processed / "odds.json"
    odds = json.loads(odds_path.read_text(encoding="utf-8")) if odds_path.exists() else None

    seasons = {}
    for season, data in standings["seasons"].items():
        odds_by_team = None
        if odds and str(odds["meta"].get("season")) == season and not data["complete"]:
            odds_by_team = {row["team_id"]: row for row in odds["teams"]}
        seasons[season] = score_season(data["teams"], elo["seasons"][season]["end"], odds_by_team)

    write_json(args.processed / "scores.json", {
        "meta": {"generated_at_utc": utc_now_iso(), "weights": WEIGHTS}, "seasons": seasons})
    latest = max(seasons)
    names = {row["team_id"]: row["abbreviation"] for row in standings["seasons"][latest]["teams"]}
    top = sorted(seasons[latest].items(), key=lambda item: -(item[1]["score"] or 0))[:5]
    print(f"Pennant Score {latest}, top 5: " + ", ".join(f"{names[int(t)]} {v['score']:.0f}" for t, v in top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
