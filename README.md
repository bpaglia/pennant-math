# Pennant Math

Data pipeline: fetch raw MLB data, validate it, and compute standings, expected records, luck, and Elo ratings.

## Setup (if you can install Python)

1. Create a free account at https://app.balldontlie.io and copy your API key.
2. Set it in your terminal (never commit it to the repository):

   ```
   export BALLDONTLIE_API_KEY=your_key_here
   ```

3. Python 3.9 or newer. There is nothing to install; the scripts use only the standard library. (On Windows, also run `pip install tzdata`.)

## Run

```
python pipeline/fetch.py --seasons 2023 2024 2025 2026
python pipeline/validate.py
python pipeline/compute.py
```

The free tier allows 5 requests per minute, so the fetch script waits about 12.5 seconds between requests. A season is roughly 26 requests (about 5 minutes), so four seasons take about 20 minutes. Completed seasons never change, so after the first run use `--skip-existing` to refetch only what is new:

```
python pipeline/fetch.py --seasons 2023 2024 2025 2026 --skip-existing
python pipeline/fetch.py --seasons 2026        # force-refresh one season
```

Output goes to `data/raw/`: `teams.json` and one `games_<season>.json` per season.

## What compute.py produces

Written to `data/processed/`:

- `standings.json`: for every season and team, the record, runs scored and allowed, run differential, Pythagenpat expected wins, **luck** (actual wins minus expected wins), one-run, extra-inning, home and road records, division rank, games back, and games remaining.
- `rolling_rd.json`: each team's run differential over its last 30 games, after every game.
- `elo.json`: Elo ratings at the start and end of each season, a per-game rating history, and diagnostics (log loss and Brier score against a home-field-only baseline) for tuning.

Only games between two MLB teams count. Standings use the regular season; Elo also learns from postseason games.

Elo starting values are K = 4, home advantage = 24 points, and a one-third pull back toward 1500 each offseason. They are starting points: try others with `--k`, `--home-advantage`, and `--carry`, and keep whichever gives a lower log loss. The first season replayed (2023) is a warm-up, because every team starts at 1500 with no prior information.

`compute.py` also stops with an error if it finds an impossible total (wins not equal to losses, runs scored not equal to runs allowed), or if a computed record does not match a verified official record listed under `official_records` in `data/known_cancellations.json`.

## Playoff odds (odds.py)

```
python pipeline/odds.py                                   # latest season, as of its last game
python pipeline/odds.py --season 2026 --as-of 2026-08-15  # any date
python pipeline/odds.py --backtest 2024 2025 2026 --sims 2000 --step-days 7
```

Needs `numpy` (`pip install -r requirements.txt`; the GitHub workflow does this for you) and the files from `compute.py`.

As of a date, games already played are fixed and every later regular-season game is simulated, 10,000 times, using each team's Elo rating on that date. Each league sends six teams to the playoffs: three division winners and three wild cards. The share of simulations in which a team gets in is its playoff probability. Ties in wins are broken by head-to-head record among the tied teams, then division record, then a random draw. This is a simplification of MLB's full tiebreak rules.

`odds.json` holds, per team: playoff, division, and first-round-bye probabilities, projected final wins (with a 10th to 90th percentile range), expected wins in the remaining games, and remaining schedule strength (average opponent Elo, adjusted for home and road games, relative to the league average; positive means a harder schedule).

**Checks built in.** Probabilities must add up to 12 playoff spots, 6 division winners, and 4 byes in every run. For a completed season, the model's final playoff field is compared with the teams that actually played in the postseason; a mismatch is reported as a warning and usually means a tiebreak rule differs from MLB's.

**Backtest.** `--backtest` replays past seasons week by week, hiding everything after each date, and scores the odds against who really made the playoffs (Brier score, lower is better) next to two simple baselines: a constant 40 percent for every team, and "in a playoff spot right now." It also prints a calibration table: of teams given about 30 percent, roughly how many got in? It writes `backtest.json` and `odds_history.json` (the playoff-odds-over-time chart data). In the GitHub workflow, tick **run_backtest** when you start a run. The first replayed season (2023) is a warm-up for the ratings, so its odds are the least reliable.

## Running without installing Python (GitHub, in the browser)

If you cannot install Python, GitHub can run everything for you:

1. Put this project in a public GitHub repository.
2. Add your API key as a secret named `BALLDONTLIE_API_KEY` (repository Settings > Secrets and variables > Actions).
3. Open the Actions tab, choose **Fetch, validate, and compute**, and click **Run workflow**.

The workflow runs the tests, fetches the data, validates it, computes the standings and ratings, simulates the playoff odds, shows the reports on the run page, and saves everything into the repository's `data/` folder. GitHub Actions is free for public repositories.

## What the validator checks

Errors stop the build (exit code 1):
- 30 teams, 15 per league, 5 per division, no duplicate ids
- no duplicate games, unknown teams, a team playing itself, or tied final scores
- no final game dated in the future
- no team with more than 162 final games (60 in 2020)
- average runs per team per game between 3.0 and 6.5 (catches parsing mistakes)

Games where neither side is one of the 30 MLB teams (for example an All-Star or exhibition game) produce a warning and are left out of every count. A game with exactly one unknown team is an error, because a real team is missing.

Verified cancellations that were never made up are listed in `data/known_cancellations.json` (with a source link for each). The validator expects those teams to be one game short and reports an INFO line instead of a warning. When you see a new short-season warning, confirm it against news or official standings before adding it to that file.

Warnings are printed but do not fail the build, because they can happen for real. Use `--strict` to fail on them too:
- a season that looks complete but has fewer than 2,430 final games, or teams with fewer than 162 (a missing game, or a cancelled game that was never replayed: compare against official standings)
- a game still marked scheduled several days after its date
- an unusually high score

## Notes

- Game times arrive in UTC. Each game also gets `date_et`, its Eastern calendar date, which is the date to use for rolling windows and daily grouping.
- Spring-training games are never fetched. Regular-season and postseason games are kept, labeled by `season_type`.
- Games not yet played come back with a status other than `final`. **Still to verify with the live API:** that a season in progress includes scheduled future games, since the playoff odds and schedule strength need them.

## Tests

```
python -m unittest discover -s tests -v
```

The tests use a local mock server and synthetic data, so they need no network access or API key.

## Next steps

1. Tune Elo (K, home advantage, preseason projections) using the backtest.
2. The page: standings table, record-vs-true-talent chart, and playoff-odds charts.
3. The scheduled daily refresh.

