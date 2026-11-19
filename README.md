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

## Running without installing Python (GitHub, in the browser)

If you cannot install Python, GitHub can run everything for you:

1. Put this project in a public GitHub repository.
2. Add your API key as a secret named `BALLDONTLIE_API_KEY` (repository Settings > Secrets and variables > Actions).
3. Open the Actions tab, choose **Fetch, validate, and compute**, and click **Run workflow**.

The workflow runs the tests, fetches the data, validates it, computes the standings and ratings, shows both reports on the run page, and saves everything into the repository's `data/` folder. GitHub Actions is free for public repositories.

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

1. Playoff odds simulation and schedule strength.
2. The page: standings table, record-vs-true-talent chart, and playoff-odds charts.
3. The scheduled daily refresh.
