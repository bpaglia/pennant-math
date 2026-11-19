"""Tests for pipeline/fetch.py and pipeline/validate.py.

Run from the project root:
    python -m unittest discover -s tests -v

No network access or API key is needed: fetch tests talk to a local mock
server, and validate tests use synthetic data.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch  # noqa: E402
import validate  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_teams():
    """30 synthetic teams: 2 leagues x 3 divisions x 5 teams."""
    teams = []
    team_id = 1
    for league in ("American", "National"):
        for division in ("East", "Central", "West"):
            for _ in range(5):
                teams.append({
                    "id": team_id,
                    "abbreviation": f"T{team_id:02d}",
                    "display_name": f"Team {team_id}",
                    "name": f"Team{team_id}",
                    "location": "City",
                    "league": league,
                    "division": division,
                })
                team_id += 1
    return teams


def raw_game(gid, date_utc="2025-04-01T23:05:00.000Z", home=1, away=2,
             home_runs=5, away_runs=3, season=2025, season_type="regular",
             state="final"):
    """A game shaped like the API response (including the bulky extras)."""
    return {
        "id": gid,
        "season": season,
        "season_type": season_type,
        "date": date_utc,
        "status": "STATUS_FINAL",
        "status_state": state,
        "home_team": {"id": home, "abbreviation": "H"},
        "away_team": {"id": away, "abbreviation": "A"},
        "home_team_data": {"hits": 8, "runs": home_runs, "errors": 0, "inning_scores": [0] * 9},
        "away_team_data": {"hits": 6, "runs": away_runs, "errors": 1, "inning_scores": [0] * 9},
        "scoring_summary": [{"play": "a long play description"}],
    }


def norm_game(gid, date_et, home, away, home_runs=5, away_runs=3, state="final",
              season=2025, season_type="regular"):
    """A game in the normalized form that fetch.py writes to disk."""
    return {
        "id": gid, "season": season, "season_type": season_type,
        "date_utc": f"{date_et}T23:00:00.000Z", "date_et": date_et,
        "status_state": state, "status": "STATUS_FINAL",
        "home_team_id": home, "away_team_id": away,
        "home_runs": home_runs, "away_runs": away_runs,
        "home_hits": 8, "away_hits": 6, "home_errors": 0, "away_errors": 0,
        "innings": 9,
    }


def complete_season(season=2025):
    """A full 2,430-game regular season where every team plays exactly 162."""
    teams = list(range(1, 31))
    fixed, rest = teams[0], teams[1:]
    rounds = []
    for _ in range(29):
        arrangement = [fixed] + rest
        rounds.append([(arrangement[i], arrangement[29 - i]) for i in range(15)])
        rest = rest[-1:] + rest[:-1]

    games, gid, start = [], 1, date(season, 3, 27)
    for r in range(162):
        for a, b in rounds[r % 29]:
            home, away = (a, b) if r % 2 == 0 else (b, a)
            away_runs = 2 + gid % 4
            home_runs = away_runs + 1 + gid % 3   # never tied
            games.append(norm_game(gid, (start + timedelta(days=r)).isoformat(),
                                   home, away, home_runs, away_runs, season=season))
            gid += 1
    return games


# --------------------------------------------------------------------------
# Mock API server
# --------------------------------------------------------------------------

class MockHandler(BaseHTTPRequestHandler):
    teams = []
    pages = {}            # (season_type, cursor or "") -> (data, next_cursor)
    rate_limit_once = False
    unauthorized = False
    log = []              # (path, Authorization header)

    def log_message(self, *args):
        pass

    def _send(self, status, body, headers=None):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        MockHandler.log.append((parsed.path, self.headers.get("Authorization")))

        if MockHandler.unauthorized:
            return self._send(401, {"error": "nope"})
        if parsed.path.endswith("/teams"):
            return self._send(200, {"data": MockHandler.teams})
        if parsed.path.endswith("/games"):
            if MockHandler.rate_limit_once:
                MockHandler.rate_limit_once = False
                return self._send(429, {"error": "slow down"}, {"Retry-After": "0"})
            season_type = query.get("season_type", [""])[0]
            cursor = query.get("cursor", [""])[0]
            data, next_cursor = MockHandler.pages.get((season_type, cursor), ([], None))
            meta = {"per_page": 100}
            if next_cursor is not None:
                meta["next_cursor"] = next_cursor
            return self._send(200, {"data": data, "meta": meta})
        return self._send(404, {"error": "not found"})


class MockServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}/mlb/v1"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockHandler.teams = make_teams()
        MockHandler.pages = {}
        MockHandler.rate_limit_once = False
        MockHandler.unauthorized = False
        MockHandler.log = []

    def client(self):
        return fetch.Client("test-key", base_url=self.base_url, min_interval=0,
                            rate_limit_wait=0, sleep=lambda seconds: None)


# --------------------------------------------------------------------------
# fetch.py
# --------------------------------------------------------------------------

class FetchHelpersTest(unittest.TestCase):
    def test_eastern_date_rolls_back_from_utc(self):
        # 8:08 PM Eastern on Oct 29 is already Oct 30 in UTC.
        self.assertEqual(fetch.to_eastern_date("2024-10-30T00:08:00.000Z"), "2024-10-29")

    def test_eastern_date_daytime_game(self):
        self.assertEqual(fetch.to_eastern_date("2025-07-04T17:05:00.000Z"), "2025-07-04")

    def test_normalize_game_keeps_needed_fields_and_drops_bulk(self):
        game = fetch.normalize_game(raw_game(7, "2024-10-30T00:08:00.000Z", home=19, away=14,
                                             home_runs=11, away_runs=4, season=2024,
                                             season_type="postseason"))
        self.assertEqual(game["id"], 7)
        self.assertEqual(game["date_et"], "2024-10-29")
        self.assertEqual((game["home_runs"], game["away_runs"]), (11, 4))
        self.assertEqual((game["home_team_id"], game["away_team_id"]), (19, 14))
        self.assertEqual(game["innings"], 9)
        self.assertNotIn("scoring_summary", game)

    def test_normalize_game_handles_unplayed_game(self):
        raw = raw_game(8, state="scheduled")
        raw["home_team_data"] = {}
        raw["away_team_data"] = None
        game = fetch.normalize_game(raw)
        self.assertIsNone(game["home_runs"])
        self.assertIsNone(game["innings"])


class FetchClientTest(MockServerTestCase):
    def test_pagination_follows_cursor_and_dedupes(self):
        MockHandler.pages = {
            ("regular", ""): ([raw_game(1), raw_game(2)], 3),
            ("regular", "3"): ([raw_game(2), raw_game(3)], None),   # id 2 repeats across pages
            ("postseason", ""): ([raw_game(4, season_type="postseason")], None),
        }
        games = fetch.fetch_season(self.client(), 2025)
        self.assertEqual([game["id"] for game in games], [1, 2, 3, 4])

    def test_sends_api_key_header(self):
        MockHandler.pages = {("regular", ""): ([raw_game(1)], None)}
        fetch.fetch_season(self.client(), 2025)
        self.assertTrue(MockHandler.log)
        self.assertTrue(all(auth == "test-key" for _, auth in MockHandler.log))

    def test_retries_after_rate_limit(self):
        MockHandler.pages = {("regular", ""): ([raw_game(1)], None)}
        MockHandler.rate_limit_once = True
        client = self.client()
        games = fetch.fetch_season(client, 2025)
        self.assertEqual(len(games), 1)
        # one 429, one retry for regular, one request for postseason
        self.assertEqual(client.requests_made, 3)

    def test_unauthorized_raises_clear_error(self):
        MockHandler.unauthorized = True
        with self.assertRaises(fetch.ApiError) as context:
            fetch.fetch_teams(self.client())
        self.assertIn("401", str(context.exception))

    def test_repeated_cursor_is_caught(self):
        MockHandler.pages = {
            ("regular", ""): ([raw_game(1)], 5),
            ("regular", "5"): ([raw_game(2)], 5),
        }
        with self.assertRaises(fetch.ApiError):
            list(fetch.paginate(self.client(), "/games", [("season_type", "regular")]))

    def test_main_writes_files_end_to_end(self):
        MockHandler.pages = {
            ("regular", ""): ([raw_game(1), raw_game(2)], None),
            ("postseason", ""): ([raw_game(3, season_type="postseason")], None),
        }
        os.environ["BALLDONTLIE_API_KEY"] = "test-key"
        with tempfile.TemporaryDirectory() as tmp:
            code = fetch.main(["--seasons", "2025", "--out", tmp,
                               "--base-url", self.base_url, "--min-interval", "0"])
            self.assertEqual(code, 0)
            teams = json.loads((Path(tmp) / "teams.json").read_text())
            games = json.loads((Path(tmp) / "games_2025.json").read_text())
            self.assertEqual(len(teams["teams"]), 30)
            self.assertEqual(games["meta"]["game_count"], 3)
            self.assertEqual(len(games["games"]), 3)

            # --skip-existing leaves the file alone
            before = (Path(tmp) / "games_2025.json").read_text()
            fetch.main(["--seasons", "2025", "--out", tmp, "--skip-existing",
                        "--base-url", self.base_url, "--min-interval", "0"])
            self.assertEqual((Path(tmp) / "games_2025.json").read_text(), before)

    def test_main_requires_api_key(self):
        os.environ.pop("BALLDONTLIE_API_KEY", None)
        self.assertEqual(fetch.main(["--seasons", "2025"]), 2)


# --------------------------------------------------------------------------
# validate.py
# --------------------------------------------------------------------------

TODAY = date(2025, 12, 1)


def run_validate(teams, games, today=TODAY, known=None):
    by_season = {}
    for game in games:
        by_season.setdefault(game["season"], []).append(game)
    return validate.validate(teams, by_season, today, known)


class ValidateTeamsTest(unittest.TestCase):
    def test_valid_league_passes(self):
        report = validate.Report()
        ids = validate.check_teams(make_teams(), report)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(ids), 30)

    def test_missing_team_fails(self):
        report = validate.Report()
        validate.check_teams(make_teams()[:-1], report)
        self.assertTrue(report.errors)

    def test_unbalanced_division_fails(self):
        teams = make_teams()
        teams[0]["division"] = "Central"   # East now 4, Central 6
        report = validate.Report()
        validate.check_teams(teams, report)
        self.assertTrue(any("Division" in message for message in report.errors))

    def test_duplicate_id_fails(self):
        teams = make_teams()
        teams[1]["id"] = teams[0]["id"]
        report = validate.Report()
        validate.check_teams(teams, report)
        self.assertTrue(any("Duplicate team id" in message for message in report.errors))


class ValidateGamesTest(unittest.TestCase):
    def test_complete_season_is_clean(self):
        report = run_validate(make_teams(), complete_season())
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])

    def test_missing_game_in_complete_season_warns(self):
        games = complete_season()[:-1]
        report = run_validate(make_teams(), games)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("expected 2430" in message for message in report.warnings))

    def test_duplicate_game_id_is_error(self):
        games = complete_season()
        games.append(dict(games[0]))
        report = run_validate(make_teams(), games)
        self.assertTrue(any("appears 2 times" in message for message in report.errors))

    def test_tied_final_is_error(self):
        games = [norm_game(1, "2025-04-01", 1, 2, 4, 4)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("tied" in message for message in report.errors))

    def test_unknown_team_is_error(self):
        games = [norm_game(1, "2025-04-01", 1, 99)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("unknown team id" in message for message in report.errors))

    def test_team_playing_itself_is_error(self):
        games = [norm_game(1, "2025-04-01", 3, 3)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("playing itself" in message for message in report.errors))

    def test_final_game_in_future_is_error(self):
        games = [norm_game(1, (TODAY + timedelta(days=5)).isoformat(), 1, 2)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("future" in message for message in report.errors))

    def test_stale_scheduled_game_warns(self):
        games = [norm_game(1, (TODAY - timedelta(days=10)).isoformat(), 1, 2,
                           None, None, state="scheduled")]
        report = run_validate(make_teams(), games)
        self.assertEqual(report.errors, [])
        self.assertTrue(any("stale" in message for message in report.warnings))

    def test_implausible_scoring_is_error(self):
        games = [norm_game(i, "2025-04-01", 1, 2, 20, 19) for i in range(1, 121)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("implausible" in message for message in report.errors))

    def test_team_over_162_games_is_error(self):
        games = [norm_game(i, "2025-04-01", 1, 2 + i % 28) for i in range(1, 164)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("more than the 162 expected" in message for message in report.errors))

    def test_game_between_non_mlb_teams_is_warning_and_excluded(self):
        # e.g. an All-Star or exhibition game reported with team ids of -1.
        games = complete_season()
        games.append(norm_game(99999, "2025-07-15", -1, -1, 4, 3))
        report = run_validate(make_teams(), games)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("not between two MLB teams", report.warnings[0])
        self.assertIn("2025-07-15", report.warnings[0])   # message says which game it is

    def test_verified_non_league_game_is_info_not_warning(self):
        games = complete_season()
        games.append(norm_game(8712499, "2025-07-15", -1, -1, 4, 0))
        by_season = {2025: games}
        report = validate.validate(make_teams(), by_season, TODAY,
                                   non_league=[{"id": 8712499}])
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertTrue(any("not between two MLB teams" in message for message in report.infos))

    def test_one_sided_unknown_team_is_still_an_error(self):
        games = [norm_game(1, "2025-04-01", 5, -1)]
        report = run_validate(make_teams(), games)
        self.assertTrue(any("unknown team id" in message for message in report.errors))

    def test_known_cancellation_explains_missing_game(self):
        games = complete_season()
        missing = games.pop()   # remove one game, as if it was cancelled
        teams_pair = sorted([missing["home_team_id"], missing["away_team_id"]])
        known = [{"season": 2025, "date": missing["date_et"], "teams": teams_pair,
                  "note": "rained out"}]
        report = run_validate(make_teams(), games, known=known)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.warnings, [])
        self.assertTrue(any("known cancellation" in message for message in report.infos))

    def test_unexplained_missing_game_still_warns(self):
        games = complete_season()
        missing = games.pop()
        other_pair = [t for t in range(1, 31) if t not in (missing["home_team_id"], missing["away_team_id"])][:2]
        known = [{"season": 2025, "date": "2025-09-01", "teams": other_pair}]
        report = run_validate(make_teams(), games, known=known)
        self.assertTrue(report.warnings)

    def test_played_game_listed_as_cancelled_is_flagged(self):
        # If the known list says a game was cancelled but all 162 were played, say so.
        games = complete_season()
        known = [{"season": 2025, "date": "2025-09-01", "teams": [1, 2]}]
        report = run_validate(make_teams(), games, known=known)
        self.assertTrue(report.errors)

    def test_shortened_2020_season_uses_its_own_totals(self):
        games = [norm_game(1, "2020-08-01", 1, 2, season=2020, state="scheduled",
                           home_runs=None, away_runs=None)]
        report = run_validate(make_teams(), games, today=date(2020, 8, 1))
        self.assertEqual(report.errors, [])


if __name__ == "__main__":
    unittest.main()
