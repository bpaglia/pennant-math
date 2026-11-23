#!/usr/bin/env python3
"""Fetch MLB teams and games from the BALLDONTLIE API into data/raw/.

Usage:
    export BALLDONTLIE_API_KEY=your_key_here
    python pipeline/fetch.py --seasons 2023 2024 2025 2026
    python pipeline/fetch.py --seasons 2026                  # refresh one season
    python pipeline/fetch.py --seasons 2023 2024 --skip-existing

Only the Python standard library is used, so there is nothing to install.

The free BALLDONTLIE tier allows 5 requests per minute, so the client spaces
requests about 12.5 seconds apart. A full season is roughly 26 requests
(about 5 minutes); backfilling four seasons takes about 20 minutes.

Output (all under data/raw/):
    teams.json            the 30 teams
    games_<season>.json   every regular-season and postseason game for a season

Spring-training games are never fetched. Each game keeps only the fields the
pipeline needs (the large play-by-play scoring summary is dropped).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://api.balldontlie.io/mlb/v1"
# Free tier: 5 requests per minute. 12.5 seconds keeps us safely under it.
DEFAULT_MIN_INTERVAL = 12.5
PER_PAGE = 100            # the API maximum
MAX_PAGES = 200           # safety valve; a season is about 25 pages
SEASON_TYPES = ("regular", "postseason")
EASTERN = ZoneInfo("America/New_York")
SOURCE_NAME = "balldontlie"
SCHEMA_VERSION = 2   # 2 = games keep their inning-by-inning scores


class ApiError(RuntimeError):
    """Raised for any API failure we cannot recover from."""


class Client:
    """Minimal API client with request spacing and retries."""

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_retries: int = 5,
        rate_limit_wait: float = 60.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.rate_limit_wait = rate_limit_wait
        self._sleep = sleep
        self._clock = clock
        self._last_request = None
        self.requests_made = 0

    def _wait_turn(self) -> None:
        if self._last_request is not None:
            wait = self.min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get(self, path: str, params=None) -> dict:
        """GET a path and return decoded JSON. `params` is a list of (key, value)."""
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(list(params))
        attempt = 0
        while True:
            self._wait_turn()
            self.requests_made += 1
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": self.api_key,
                    "Accept": "application/json",
                    "User-Agent": "pennant-math/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                err.close()  # release the connection before deciding what to do
                if err.code == 401:
                    raise ApiError(
                        "401 Unauthorized: check BALLDONTLIE_API_KEY, and that "
                        "your account tier includes this endpoint."
                    ) from err
                if err.code in (400, 404, 406):
                    raise ApiError(f"HTTP {err.code} for {url}") from err
                if err.code == 429 or err.code >= 500:
                    attempt += 1
                    if attempt > self.max_retries:
                        raise ApiError(
                            f"HTTP {err.code} for {url} after {self.max_retries} retries"
                        ) from err
                    self._sleep(self._retry_delay(err, attempt))
                    continue
                raise ApiError(f"HTTP {err.code} for {url}") from err
            except (urllib.error.URLError, TimeoutError) as err:
                attempt += 1
                if attempt > self.max_retries:
                    raise ApiError(f"Network error for {url}: {err}") from err
                self._sleep(min(2.0 ** attempt, 60.0))

    def _retry_delay(self, err: urllib.error.HTTPError, attempt: int) -> float:
        retry_after = err.headers.get("Retry-After") if err.headers else None
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        if err.code == 429:
            return self.rate_limit_wait
        return min(2.0 ** attempt, 60.0)


def paginate(client: Client, path: str, params):
    """Yield every item from a cursor-paginated endpoint."""
    cursor = None
    seen_cursors = set()
    for _ in range(MAX_PAGES):
        query = list(params) + [("per_page", PER_PAGE)]
        if cursor is not None:
            query.append(("cursor", cursor))
        page = client.get(path, query)
        yield from page.get("data", [])
        cursor = (page.get("meta") or {}).get("next_cursor")
        if cursor is None:
            return
        if cursor in seen_cursors:
            raise ApiError(f"Cursor {cursor} repeated for {path}; stopping to avoid a loop")
        seen_cursors.add(cursor)
    raise ApiError(f"More than {MAX_PAGES} pages for {path}; stopping")


def to_eastern_date(date_utc: str) -> str:
    """Convert an API timestamp (UTC) to the game's Eastern calendar date.

    The API reports start times in UTC, so a 7 PM Eastern game on Oct 29 shows
    up as Oct 30 in UTC. Eastern dates match how fans think about game days.
    """
    moment = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
    return moment.astimezone(EASTERN).date().isoformat()


def normalize_team(team: dict) -> dict:
    return {
        "id": team["id"],
        "abbreviation": team["abbreviation"],
        "display_name": team["display_name"],
        "name": team["name"],
        "location": team["location"],
        "league": team["league"],
        "division": team["division"],
    }


def normalize_game(game: dict) -> dict:
    home_data = game.get("home_team_data") or {}
    away_data = game.get("away_team_data") or {}
    inning_counts = [
        len(home_data.get("inning_scores") or []),
        len(away_data.get("inning_scores") or []),
    ]
    return {
        "id": game["id"],
        "season": game["season"],
        "season_type": game.get("season_type"),
        "date_utc": game["date"],
        "date_et": to_eastern_date(game["date"]),
        "status_state": game.get("status_state"),
        "status": game.get("status"),
        "home_team_id": game["home_team"]["id"],
        "away_team_id": game["away_team"]["id"],
        "home_runs": home_data.get("runs"),
        "away_runs": away_data.get("runs"),
        "home_hits": home_data.get("hits"),
        "away_hits": away_data.get("hits"),
        "home_errors": home_data.get("errors"),
        "away_errors": away_data.get("errors"),
        "innings": max(inning_counts) or None,
        "home_innings": home_data.get("inning_scores"),
        "away_innings": away_data.get("inning_scores"),
    }


def fetch_teams(client: Client) -> list:
    response = client.get("/teams")
    teams = [normalize_team(team) for team in response.get("data", [])]
    return sorted(teams, key=lambda team: team["id"])


def fetch_season(client: Client, season: int) -> list:
    """Fetch every regular-season and postseason game for one season."""
    games = {}
    for season_type in SEASON_TYPES:
        params = [("seasons[]", season), ("season_type", season_type)]
        for raw_game in paginate(client, "/games", params):
            game = normalize_game(raw_game)
            games[game["id"]] = game  # dedupe across page boundaries
    return sorted(games.values(), key=lambda game: (game["date_utc"], game["id"]))


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)


def _has_current_schema(path: Path) -> bool:
    """True if a saved games file already has everything the pipeline now needs."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("meta", {}).get("schema") == SCHEMA_VERSION
    except (OSError, ValueError):
        return False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--seasons",
        nargs="+",
        type=int,
        default=[datetime.now(timezone.utc).year],
        help="Seasons to fetch (default: the current calendar year).",
    )
    parser.add_argument("--out", type=Path, default=Path("data/raw"), help="Output folder.")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip seasons whose games file already exists (useful for completed seasons).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL,
        help="Seconds between requests (default %(default)s, safe for the free tier).",
    )
    parser.add_argument(
        "--base-url",
        default=BASE_URL,
        help="Override the API base URL (used by the tests).",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    api_key = os.environ.get("BALLDONTLIE_API_KEY", "").strip()
    if not api_key:
        print("Set the BALLDONTLIE_API_KEY environment variable first.", file=sys.stderr)
        return 2

    client = Client(api_key, base_url=args.base_url, min_interval=args.min_interval)
    try:
        teams = fetch_teams(client)
        write_json_atomic(
            args.out / "teams.json",
            {"meta": {"source": SOURCE_NAME, "fetched_at_utc": utc_now_iso()}, "teams": teams},
        )
        print(f"teams: {len(teams)}")

        for season in args.seasons:
            path = args.out / f"games_{season}.json"
            if args.skip_existing and path.exists() and _has_current_schema(path):
                print(f"{season}: skipped (file exists)")
                continue
            games = fetch_season(client, season)
            write_json_atomic(
                path,
                {
                    "meta": {
                        "source": SOURCE_NAME,
                        "schema": SCHEMA_VERSION,
                        "season": season,
                        "fetched_at_utc": utc_now_iso(),
                        "game_count": len(games),
                    },
                    "games": games,
                },
            )
            print(f"{season}: {len(games)} games written to {path}")
    except ApiError as err:
        print(f"Fetch failed: {err}", file=sys.stderr)
        return 1

    print(f"done ({client.requests_made} requests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
