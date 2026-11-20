"""Tests for pipeline/compute.py.

Run from the project root:
    python -m unittest discover -s tests -v
"""
import json
import math
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tests"))

import compute  # noqa: E402
from test_pipeline import complete_season, make_teams, norm_game  # noqa: E402

TEAMS = make_teams()
TEAM_IDS = {team["id"] for team in TEAMS}


def row_for(standings, team_id):
    return next(row for row in standings["teams"] if row["team_id"] == team_id)


class PythagenpatTest(unittest.TestCase):
    def test_matches_hand_calculation(self):
        # Worked by hand: exponent 1.8941, expected win% 0.5629.
        self.assertAlmostEqual(compute.pythagenpat_win_pct(800, 700, 162), 0.5629, places=3)

    def test_even_runs_is_exactly_half(self):
        self.assertAlmostEqual(compute.pythagenpat_win_pct(700, 700, 162), 0.5, places=9)

    def test_shutout_scoring_edge_cases(self):
        self.assertEqual(compute.pythagenpat_win_pct(0, 0, 10), 0.5)
        self.assertEqual(compute.pythagenpat_win_pct(5, 0, 1), 1.0)


class StandingsTest(unittest.TestCase):
    def standings(self, games):
        return compute.compute_standings(games, TEAMS)

    def test_record_runs_and_splits(self):
        games = [
            norm_game(1, "2025-04-01", home=1, away=2, home_runs=3, away_runs=2),      # team 1 wins at home by one run
            norm_game(2, "2025-04-02", home=2, away=1, home_runs=7, away_runs=1),      # team 1 loses on the road
            norm_game(3, "2025-04-03", home=1, away=2, home_runs=4, away_runs=3),      # team 1 wins at home by one run
        ]
        games[2]["innings"] = 11                                                        # extra innings
        row = row_for(self.standings(games), 1)
        self.assertEqual((row["wins"], row["losses"]), (2, 1))
        self.assertEqual((row["runs_scored"], row["runs_allowed"], row["run_diff"]), (8, 12, -4))
        self.assertEqual((row["one_run_wins"], row["one_run_losses"]), (2, 0))
        self.assertEqual((row["extra_wins"], row["extra_losses"]), (1, 0))
        self.assertEqual((row["home_wins"], row["home_losses"]), (2, 0))
        self.assertEqual((row["road_wins"], row["road_losses"]), (0, 1))
        self.assertEqual(row["win_pct"], round(2 / 3, 4))

    def test_luck_is_actual_minus_expected_wins(self):
        games = [norm_game(i, "2025-04-01", 1, 2, 2, 1) for i in range(1, 4)]          # 3 narrow wins
        games.append(norm_game(4, "2025-04-02", 1, 2, 1, 9))                             # one blowout loss
        row = row_for(self.standings(games), 1)
        expected = compute.pythagenpat_win_pct(row["runs_scored"], row["runs_allowed"], 4)
        self.assertAlmostEqual(row["luck"], round(3 - expected * 4, 2), places=2)
        self.assertGreater(row["luck"], 0)   # narrow wins and a blowout loss means lucky

    def test_postseason_non_final_and_non_league_games_are_excluded(self):
        games = [
            norm_game(1, "2025-04-01", 1, 2, 5, 3),
            norm_game(2, "2025-10-01", 1, 2, 9, 0, season_type="postseason"),
            norm_game(3, "2025-04-02", 1, 2, None, None, state="postponed"),
            norm_game(4, "2025-07-15", -1, -1, 4, 0),
        ]
        standings = self.standings(games)
        row = row_for(standings, 1)
        self.assertEqual((row["games"], row["runs_scored"]), (1, 5))
        self.assertEqual(sum(r["games"] for r in standings["teams"]), 2)   # one game, two teams

    def test_games_remaining_and_complete_flag(self):
        games = [
            norm_game(1, "2025-04-01", 1, 2, 5, 3),
            norm_game(2, "2025-04-02", 1, 3, None, None, state="scheduled"),
        ]
        standings = self.standings(games)
        self.assertEqual(row_for(standings, 1)["games_remaining"], 1)
        self.assertEqual(row_for(standings, 3)["games_remaining"], 1)
        self.assertFalse(standings["complete"])

    def test_complete_season_identities(self):
        standings = self.standings(complete_season())
        compute.check_integrity(2025, standings["teams"])           # must not raise
        self.assertTrue(standings["complete"])
        self.assertTrue(all(row["games"] == 162 for row in standings["teams"]))
        self.assertEqual(sum(row["wins"] for row in standings["teams"]), 2430)
        self.assertEqual(sum(row["runs_scored"] for row in standings["teams"]),
                         sum(row["runs_allowed"] for row in standings["teams"]))

    def test_division_rank_and_games_back(self):
        games = [
            norm_game(1, "2025-04-01", 1, 6, 5, 1),   # team 1 beats a different-division team: 1-0
            norm_game(2, "2025-04-02", 2, 6, 5, 1),   # team 2: 1-0
            norm_game(3, "2025-04-03", 2, 6, 5, 1),   # team 2: 2-0
            norm_game(4, "2025-04-04", 3, 6, 1, 5),   # team 3: 0-1
        ]
        standings = self.standings(games)
        self.assertEqual(row_for(standings, 2)["division_rank"], 1)
        self.assertEqual(row_for(standings, 2)["games_back"], 0)
        self.assertEqual(row_for(standings, 1)["division_rank"], 2)
        self.assertEqual(row_for(standings, 1)["games_back"], 0.5)   # 1-0 behind 2-0
        self.assertEqual(row_for(standings, 3)["games_back"], 1.5)   # 0-1 behind 2-0

    def test_integrity_check_catches_broken_data(self):
        standings = self.standings([norm_game(1, "2025-04-01", 1, 2, 5, 3)])
        standings["teams"][0]["wins"] += 1
        with self.assertRaises(compute.ComputeError):
            compute.check_integrity(2025, standings["teams"])


class RollingTest(unittest.TestCase):
    def test_window_drops_old_games(self):
        # Team 1 plays 35 home games: the first five are won by 10 runs, the rest by 1.
        games = []
        for i in range(1, 36):
            day = (date(2025, 4, 1) + timedelta(days=i)).isoformat()
            home_runs = 11 if i <= 5 else 2
            games.append(norm_game(i, day, 1, 2 + i % 28, home_runs, 1))
        finals = compute.league_final_games(games, TEAM_IDS)
        rolling = compute.compute_rolling(finals)["1"]
        self.assertEqual(rolling[0][:1] + [rolling[0][2]], [1, 10])
        self.assertEqual(rolling[29][2], 5 * 10 + 25 * 1)    # games 1-30 all in the window
        self.assertEqual(rolling[34][2], 30 * 1)              # the five +10 games have dropped out
        self.assertEqual(rolling[34][0], 35)                  # game counter

    def test_away_games_use_the_opposite_sign(self):
        finals = compute.league_final_games([norm_game(1, "2025-04-01", 1, 2, 6, 2)], TEAM_IDS)
        rolling = compute.compute_rolling(finals)
        self.assertEqual(rolling["1"][0][2], 4)
        self.assertEqual(rolling["2"][0][2], -4)


class EloTest(unittest.TestCase):
    PARAMS = {"k": 4.0, "home_advantage": 24.0, "carry": 2 / 3}

    def test_single_game_matches_hand_calculation(self):
        games = {2025: [norm_game(1, "2025-04-01", 1, 2, 4, 3)]}      # equal teams, home wins by one
        result = compute.run_elo(games, TEAM_IDS, self.PARAMS)
        end = result["seasons"]["2025"]["end"]
        # Worked by hand: expected home win 0.5345, change = 4 x ln(2) x (1 - 0.5345) = 1.2907
        self.assertAlmostEqual(end["1"], 1501.3, places=1)
        self.assertAlmostEqual(end["2"], 1498.7, places=1)

    def test_ratings_are_zero_sum(self):
        result = compute.run_elo({2025: complete_season()}, TEAM_IDS, self.PARAMS)
        total = sum(result["seasons"]["2025"]["end"].values())
        self.assertAlmostEqual(total, 30 * 1500, delta=1.0)   # rounding to 0.1 per team

    def test_bigger_margin_moves_ratings_more(self):
        small = compute.run_elo({2025: [norm_game(1, "2025-04-01", 1, 2, 4, 3)]}, TEAM_IDS, self.PARAMS)
        big = compute.run_elo({2025: [norm_game(1, "2025-04-01", 1, 2, 12, 3)]}, TEAM_IDS, self.PARAMS)
        self.assertGreater(big["seasons"]["2025"]["end"]["1"], small["seasons"]["2025"]["end"]["1"])

    def test_upset_moves_ratings_more_than_expected_result(self):
        games = {2025: [norm_game(1, "2025-04-01", 1, 2, 5, 1),     # team 1 builds a lead
                        norm_game(2, "2025-04-02", 1, 2, 5, 1)]}
        base = compute.run_elo(games, TEAM_IDS, self.PARAMS)["seasons"]["2025"]["end"]["1"]
        games[2025].append(norm_game(3, "2025-04-03", 1, 2, 1, 5))   # now team 1 loses
        after = compute.run_elo(games, TEAM_IDS, self.PARAMS)["seasons"]["2025"]["end"]["1"]
        self.assertLess(after, base)

    def test_offseason_regression_toward_the_average(self):
        games = {2025: [norm_game(1, "2025-04-01", 1, 2, 12, 1)],
                 2026: [norm_game(2, "2026-04-01", 3, 4, 2, 1, season=2026)]}
        result = compute.run_elo(games, TEAM_IDS, self.PARAMS)
        end_2025 = result["seasons"]["2025"]["end"]["1"]
        start_2026 = result["seasons"]["2026"]["start"]["1"]
        self.assertAlmostEqual(start_2026, 1500 + (end_2025 - 1500) * (2 / 3), places=0)
        self.assertLess(abs(start_2026 - 1500), abs(end_2025 - 1500))

    def test_postseason_updates_ratings_but_not_diagnostics(self):
        games = {2025: [norm_game(1, "2025-04-01", 1, 2, 4, 3),
                        norm_game(2, "2025-10-05", 1, 2, 9, 1, season_type="postseason")]}
        result = compute.run_elo(games, TEAM_IDS, self.PARAMS)
        self.assertEqual(result["diagnostics"]["2025"]["games"], 1)
        self.assertEqual(len(result["seasons"]["2025"]["history"]["1"]), 2)

    def test_non_league_games_do_not_touch_ratings(self):
        games = {2025: [norm_game(1, "2025-07-15", -1, -1, 4, 0)]}
        result = compute.run_elo(games, TEAM_IDS, self.PARAMS)
        self.assertTrue(all(rating == 1500.0 for rating in result["seasons"]["2025"]["end"].values()))

    def test_expected_win_probability(self):
        self.assertAlmostEqual(compute.expected_home_win(1500, 1500, 24), 0.5345, places=3)
        self.assertAlmostEqual(compute.expected_home_win(1500, 1500, 0), 0.5, places=9)
        self.assertGreater(compute.expected_home_win(1600, 1500, 24), 0.64)

    def test_diagnostics_are_sane(self):
        result = compute.run_elo({2025: complete_season()}, TEAM_IDS, self.PARAMS)
        diag = result["diagnostics"]["2025"]
        self.assertEqual(diag["games"], 2430)
        self.assertTrue(0 < diag["log_loss"] < 2)
        self.assertTrue(math.isfinite(diag["brier"]))


class OfficialRecordsTest(unittest.TestCase):
    def setUp(self):
        self.standings = {"2025": compute.compute_standings(complete_season(), TEAMS)}
        self.row = row_for(self.standings["2025"], 1)

    def test_matching_record_passes(self):
        record = {"season": 2025, "team_id": 1, "wins": self.row["wins"], "losses": self.row["losses"]}
        ok, lines = compute.check_official_records(self.standings, [record])
        self.assertTrue(ok)
        self.assertTrue(lines[0].startswith("OK"))

    def test_mismatching_record_fails(self):
        record = {"season": 2025, "team_id": 1, "wins": self.row["wins"] + 1, "losses": self.row["losses"] - 1}
        ok, lines = compute.check_official_records(self.standings, [record])
        self.assertFalse(ok)
        self.assertTrue(lines[0].startswith("FAIL"))

    def test_missing_season_is_skipped_not_failed(self):
        ok, lines = compute.check_official_records(self.standings, [{"season": 1999, "team_id": 1, "wins": 1, "losses": 1}])
        self.assertTrue(ok)
        self.assertTrue(lines[0].startswith("SKIP"))


class BatchOneFieldsTest(unittest.TestCase):
    def test_home_and_road_run_differential(self):
        games = [norm_game(1, "2025-04-01", 1, 2, 5, 3), norm_game(2, "2025-04-02", 2, 1, 4, 1)]
        row = row_for(compute.compute_standings(games, TEAMS), 1)
        self.assertEqual((row["home_run_diff"], row["road_run_diff"]), (2, -3))

    def test_one_run_z_flags_unusual_close_game_records(self):
        games = [norm_game(i, "2025-04-01", 1, 2 + i, 3, 2) for i in range(1, 5)]      # four one-run wins
        row = row_for(compute.compute_standings(games, TEAMS), 1)
        self.assertEqual(row["one_run_z"], 2.0)
        self.assertIsNone(row_for(compute.compute_standings([], TEAMS), 1)["one_run_z"])

    def test_record_against_teams_above_500(self):
        games = [norm_game(1, "2025-04-01", 2, 3, 5, 1), norm_game(2, "2025-04-02", 2, 3, 5, 1),
                 norm_game(3, "2025-04-03", 2, 1, 5, 1), norm_game(4, "2025-04-04", 1, 3, 5, 1)]
        standings = compute.compute_standings(games, TEAMS)     # team 2 is 3-0, the only team above .500 besides team 1 (1-1)
        self.assertEqual((row_for(standings, 1)["vs_winning_wins"], row_for(standings, 1)["vs_winning_losses"]), (0, 1))
        self.assertEqual((row_for(standings, 3)["vs_winning_wins"], row_for(standings, 3)["vs_winning_losses"]), (0, 2))
        self.assertEqual((row_for(standings, 2)["vs_winning_wins"], row_for(standings, 2)["vs_winning_losses"]), (0, 0))

    def test_schedule_played_is_relative_to_the_league_average(self):
        ratings = {str(i): 1500.0 for i in range(1, 31)}
        ratings["1"], ratings["2"] = 1600.0, 1400.0
        games = [norm_game(1, "2025-04-01", 1, 2, 5, 3)]
        standings = compute.compute_standings(games, TEAMS)
        compute.add_sos_played(standings, games, ratings, TEAM_IDS)
        self.assertEqual(row_for(standings, 1)["sos_played"], -100.0)     # faced the 1400 team
        self.assertEqual(row_for(standings, 2)["sos_played"], 100.0)      # faced the 1600 team
        self.assertIsNone(row_for(standings, 5)["sos_played"])


class EndToEndTest(unittest.TestCase):
    def test_main_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            games = complete_season(2025)
            games.append(norm_game(999999, "2025-07-15", -1, -1, 4, 0))   # an All-Star-style game
            (raw / "teams.json").write_text(json.dumps({"teams": TEAMS}))
            (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": games}))
            row = row_for(compute.compute_standings(games, TEAMS), 1)
            (root / "known_cancellations.json").write_text(json.dumps({"official_records": [
                {"season": 2025, "team_id": 1, "wins": row["wins"], "losses": row["losses"]}]}))
            out = root / "processed"

            code = compute.main(["--raw", str(raw), "--out", str(out)])
            self.assertEqual(code, 0)

            standings = json.loads((out / "standings.json").read_text())
            teams = standings["seasons"]["2025"]["teams"]
            self.assertEqual(len(teams), 30)
            self.assertTrue(all(team["games"] == 162 for team in teams))
            rolling = json.loads((out / "rolling_rd.json").read_text())
            self.assertEqual(len(rolling["seasons"]["2025"]["1"]), 162)
            elo = json.loads((out / "elo.json").read_text())
            self.assertEqual(len(elo["seasons"]["2025"]["end"]), 30)

    def test_main_fails_when_official_record_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            (raw / "teams.json").write_text(json.dumps({"teams": TEAMS}))
            (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": complete_season(2025)}))
            (root / "known_cancellations.json").write_text(json.dumps({"official_records": [
                {"season": 2025, "team_id": 1, "wins": 1, "losses": 161}]}))
            self.assertEqual(compute.main(["--raw", str(raw), "--out", str(root / "out")]), 1)

    def test_main_reports_missing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(compute.main(["--raw", str(Path(tmp) / "nope"), "--out", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
