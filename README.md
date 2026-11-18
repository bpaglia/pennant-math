# Pennant Math

Data pipeline, step 1: fetch raw MLB data and validate it.

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
```

The free tier allows 5 requests per minute, so the fetch script waits about 12.5 seconds between requests. A season is roughly 26 requests (about 5 minutes), so four seasons take about 20 minutes. Completed seasons never change, so after the first run use `--skip-existing` to refetch only what is new:

```
python pipeline/fetch.py --seasons 2023 2024 2025 2026 --skip-existing
python pipeline/fetch.py --seasons 2026        # force-refresh one season
```

Output goes to `data/raw/`: `teams.json` and one `games_<season>.json` per season.

## Running without installing Python (GitHub, in the browser)

If you cannot install Python, GitHub can run everything for you:

1. Put this project in a public GitHub repository.
2. Add your API key as a secret named `BALLDONTLIE_API_KEY` (repository Settings > Secrets and variables > Actions).
3. Open the Actions tab, choose **Fetch and validate data**, and click **Run workflow**.

The workflow runs the tests, fetches the data, validates it, shows the validation results on the run page, and saves the data files into the repository's `data/` folder. GitHub Actions is free for public repositories.

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

1. `pipeline/compute.py`: standings, run differential, Pythagenpat, luck, Elo.
2. Playoff odds simulation and schedule strength.
3. The page, then the scheduled GitHub Action.
