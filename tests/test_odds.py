"""Tests for pipeline/odds.py (playoff odds, tiebreaks, projections, backtest).

Run from the project root:
    python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tests"))

import compute  # noqa: E402
import odds  # noqa: E402
from test_pipeline import complete_season, make_teams, norm_game  # noqa: E402

TEAMS = make_teams()          # ids 1-30; AL East 1-5, Central 6-10, West 11-15; NL East 16-20, Central 21-25, West 26-30
IDS = [team["id"] for team in TEAMS]
EQUAL = {team_id: 1500.0 for team_id in IDS}
TEAM_ID_SET = set(IDS)


def ordered_season():
    """One game per pair of teams; the lower team id always wins. Team 1 wins 29, team 30 wins 0."""
    games, gid = [], 1
    for i in range(1, 31):
        for j in range(i + 1, 31):
            games.append(norm_game(gid, "2025-06-01", home=i, away=j, home_runs=5, away_runs=3))
            gid += 1
    return games


def blank_rng():
    return np.random.default_rng(0)


class SeasonStateTest(unittest.TestCase):
    def test_as_of_splits_played_from_unplayed(self):
        games = complete_season()
        state = odds.SeasonState(TEAMS, games, "2025-06-01")
        played = sum(1 for g in games if g["date_et"] <= "2025-06-01")
        self.assertEqual(int(state.wins.sum()), played)
        self.assertEqual(state.G, 2430 - played)      # results after the date are hidden, games still to play

    def test_before_the_season_nothing_is_played(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-01-01")
        self.assertEqual(int(state.wins.sum()), 0)
        self.assertEqual(state.G, 2430)

    def test_cancelled_and_postponed_games_are_not_on_the_schedule(self):
        games = [norm_game(1, "2025-06-01", 1, 2, None, None, state="postponed"),
                 norm_game(2, "2025-06-02", 1, 2, None, None, state="canceled"),
                 norm_game(3, "2025-06-03", 1, 2, None, None, state="scheduled")]
        state = odds.SeasonState(TEAMS, games, "2025-05-01")
        self.assertEqual(state.G, 1)

    def test_non_league_and_postseason_games_are_ignored(self):
        games = [norm_game(1, "2025-07-15", -1, -1, 4, 0),
                 norm_game(2, "2025-10-01", 1, 2, 5, 3, season_type="postseason")]
        state = odds.SeasonState(TEAMS, games, "2025-12-31")
        self.assertEqual(int(state.wins.sum()), 0)
        self.assertEqual(state.G, 0)

    def test_head_to_head_is_recorded(self):
        state = odds.SeasonState(TEAMS, [norm_game(1, "2025-06-01", 1, 2, 5, 3)], "2025-12-31")
        self.assertEqual(int(state.h2h[0, 1]), 1)
        self.assertEqual(int(state.h2h[1, 0]), 0)


class RatingsAsOfTest(unittest.TestCase):
    ELO = {"seasons": {"2025": {
        "start": {str(i): 1500.0 for i in IDS},
        "history": {"1": [["2025-04-01", 1510.0], ["2025-04-10", 1520.0], ["2025-05-01", 1490.0]]},
    }}}

    def test_before_first_game_uses_the_start_rating(self):
        self.assertEqual(odds.ratings_as_of(self.ELO, 2025, "2025-03-30", [1])[1], 1500.0)

    def test_uses_the_latest_rating_on_or_before_the_date(self):
        self.assertEqual(odds.ratings_as_of(self.ELO, 2025, "2025-04-10", [1])[1], 1520.0)   # inclusive
        self.assertEqual(odds.ratings_as_of(self.ELO, 2025, "2025-04-20", [1])[1], 1520.0)
        self.assertEqual(odds.ratings_as_of(self.ELO, 2025, "2025-06-01", [1])[1], 1490.0)

    def test_team_without_history_keeps_its_start_rating(self):
        self.assertEqual(odds.ratings_as_of(self.ELO, 2025, "2025-06-01", [2])[2], 1500.0)


class TiebreakTest(unittest.TestCase):
    def rank(self, games, indices):
        state = odds.SeasonState(TEAMS, games, "2025-12-31")
        return odds._rank_exact(state, indices, state.wins, np.zeros(0, dtype=bool), blank_rng())

    def test_head_to_head_breaks_a_two_team_tie(self):
        games = [
            norm_game(1, "2025-06-01", 1, 2, 5, 3),      # team 1 beats team 2
            norm_game(2, "2025-06-02", 1, 6, 3, 5),      # team 1 loses to an outside team
            norm_game(3, "2025-06-03", 2, 7, 5, 3),      # team 2 beats an outside team
        ]                                                 # teams 1 and 2 are both 1-1
        self.assertEqual(self.rank(games, [0, 1]), [0, 1])
        self.assertEqual(self.rank(games, [1, 0]), [0, 1])

    def test_head_to_head_can_favor_the_other_team(self):
        games = [
            norm_game(1, "2025-06-01", 2, 1, 5, 3),      # team 2 beats team 1
            norm_game(2, "2025-06-02", 1, 6, 5, 3),      # team 1 beats an outside team
            norm_game(3, "2025-06-03", 2, 7, 3, 5),      # team 2 loses to an outside team
        ]
        self.assertEqual(self.rank(games, [0, 1]), [1, 0])

    def test_division_record_breaks_a_tie_with_no_head_to_head(self):
        games = [
            norm_game(1, "2025-06-01", 1, 3, 5, 3),      # team 1 beats a division rival
            norm_game(2, "2025-06-02", 1, 6, 3, 5),      # and loses outside the division: 1-1, division 1-0
            norm_game(3, "2025-06-03", 2, 4, 3, 5),      # team 2 loses to a division rival
            norm_game(4, "2025-06-04", 2, 7, 5, 3),      # and wins outside: 1-1, division 0-1
        ]
        self.assertEqual(self.rank(games, [1, 0]), [0, 1])

    def test_more_wins_always_beats_tiebreakers(self):
        games = [norm_game(1, "2025-06-01", 2, 1, 5, 3),      # team 2 beats team 1 (head-to-head)
                 norm_game(2, "2025-06-02", 1, 6, 5, 3),
                 norm_game(3, "2025-06-03", 1, 7, 5, 3)]      # but team 1 has 2 wins to team 2's 1
        self.assertEqual(self.rank(games, [1, 0])[0], 0)


class FieldTest(unittest.TestCase):
    def test_ordered_season_gives_the_expected_field(self):
        field = odds.final_field(TEAMS, ordered_season())
        # Division winners are the lowest ids in each division; wild cards are the next best three per league.
        self.assertEqual(field["division_winners"], {1, 6, 11, 16, 21, 26})
        self.assertEqual(field["playoffs"], {1, 2, 3, 4, 6, 11, 16, 17, 18, 19, 21, 26})

    def test_byes_go_to_the_two_best_division_winners(self):
        state = odds.SeasonState(TEAMS, ordered_season(), "2025-12-31")
        result = odds.simulate(state, EQUAL, n_sims=5, seed=3)
        byes = {state.ids[i] for i in range(30) if result["p_bye"][i] > 0.5}
        self.assertEqual(byes, {1, 6, 16, 21})

    def test_vectorized_path_agrees_with_the_exact_path_when_there_are_no_ties(self):
        state = odds.SeasonState(TEAMS, ordered_season(), "2025-12-31")
        result = odds.simulate(state, EQUAL, n_sims=10, seed=5)
        self.assertEqual({state.ids[i] for i in range(30) if result["p_playoffs"][i] == 1.0},
                         {1, 2, 3, 4, 6, 11, 16, 17, 18, 19, 21, 26})
        self.assertTrue(set(np.round(result["p_playoffs"], 6)) <= {0.0, 1.0})

    def test_postseason_teams_come_from_postseason_games(self):
        games = [norm_game(1, "2025-10-01", 1, 2, 5, 3, season_type="postseason"),
                 norm_game(2, "2025-10-02", 3, 1, 5, 3, season_type="postseason"),
                 norm_game(3, "2025-07-15", -1, -1, 4, 0, season_type="postseason")]
        self.assertEqual(odds.postseason_teams(games, TEAM_ID_SET), {1, 2, 3})


class SimulationTest(unittest.TestCase):
    def test_equal_teams_each_have_about_twelve_in_thirty(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-01-01")
        result = odds.simulate(state, EQUAL, n_sims=3000, seed=11)
        self.assertTrue(np.all(np.abs(result["p_playoffs"] - 0.4) < 0.045))
        self.assertTrue(np.all(np.abs(result["p_division"] - 0.2) < 0.04))
        self.assertAlmostEqual(float(result["wins_mean"].mean()), 81.0, places=1)

    def test_probabilities_add_up_to_the_size_of_the_field(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-07-01")
        result = odds.simulate(state, EQUAL, n_sims=1000, seed=2)
        self.assertAlmostEqual(float(result["p_playoffs"].sum()), 12.0, places=6)
        self.assertAlmostEqual(float(result["p_division"].sum()), 6.0, places=6)
        self.assertAlmostEqual(float(result["p_bye"].sum()), 4.0, places=6)

    def test_each_league_gets_six_playoff_teams(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-07-01")
        result = odds.simulate(state, EQUAL, n_sims=500, seed=4)
        al = sum(result["p_playoffs"][i] for i in range(30) if state.league[i] == "American")
        self.assertAlmostEqual(float(al), 6.0, places=6)

    def test_a_much_stronger_team_is_very_likely_to_win_its_division(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-01-01")
        ratings = dict(EQUAL)
        ratings[1] = 1800.0
        result = odds.simulate(state, ratings, n_sims=1000, seed=7)
        self.assertGreater(result["p_division"][0], 0.95)
        self.assertGreater(result["p_playoffs"][0], 0.99)
        self.assertGreater(result["wins_mean"][0], 110)

    def test_fast_path_matches_a_slow_exact_reference(self):
        # Tight mid-season races make many ties. Resolve every simulation exactly and compare.
        state = odds.SeasonState(TEAMS, complete_season(), "2025-08-15")
        n_sims = 600
        rng = np.random.default_rng(21)
        p_home = odds.home_win_probabilities(state, EQUAL, 24.0)
        hw = rng.random((n_sims, state.G)) < p_home
        wins = (state.wins + np.rint(hw.astype(np.float32) @ state.D + state.base)).astype(np.int32)

        fast_div, fast_bye, fast_po = odds._classify(state, wins, hw, np.random.default_rng(1))
        slow_div = np.zeros_like(fast_div)
        slow_po = np.zeros_like(fast_po)
        ref_rng = np.random.default_rng(2)
        for s in range(n_sims):
            for league in state.league_divisions:
                winners, byes, wild = odds.resolve_league_exact(state, league, wins[s], hw[s], ref_rng)
                slow_div[s, winners] = True
                slow_po[s, winners + wild] = True
        self.assertLess(float(np.abs(fast_div.mean(axis=0) - slow_div.mean(axis=0)).max()), 0.06)
        self.assertLess(float(np.abs(fast_po.mean(axis=0) - slow_po.mean(axis=0)).max()), 0.06)

    def test_same_seed_gives_the_same_answer(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-07-01")
        a = odds.simulate(state, EQUAL, n_sims=300, seed=99)
        b = odds.simulate(state, EQUAL, n_sims=300, seed=99)
        self.assertTrue(np.array_equal(a["p_playoffs"], b["p_playoffs"]))

    def test_a_clinched_team_has_probability_one(self):
        # Team 1 has already won 29 games and its division rivals have at most 28, with one game left.
        games = ordered_season() + [norm_game(9000, "2025-12-15", 2, 3, None, None, state="scheduled")]
        state = odds.SeasonState(TEAMS, games, "2025-12-01")
        result = odds.simulate(state, EQUAL, n_sims=200, seed=1)
        self.assertEqual(result["p_division"][0], 1.0)

    def test_invariant_check_catches_a_broken_result(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-07-01")
        result = odds.simulate(state, EQUAL, n_sims=50, seed=1)
        result["p_playoffs"] = result["p_playoffs"] * 0.5
        with self.assertRaises(odds.OddsError):
            odds.check_invariants(state, result)


class ProjectionTest(unittest.TestCase):
    def test_expected_wins_remaining_and_schedule_strength(self):
        state = odds.SeasonState(TEAMS, complete_season(), "2025-06-01")
        extra = odds.schedule_and_projection(state, EQUAL, 24.0)
        self.assertTrue(np.all(extra["games_remaining"] > 0))
        # Equal teams: about half the remaining games are won, a bit more at home.
        for i in range(30):
            self.assertAlmostEqual(extra["expected_wins_remaining"][i] / extra["games_remaining"][i], 0.5, delta=0.03)
        self.assertTrue(np.all(np.abs(extra["sos_remaining"]) < 3.0))

    def test_tougher_opponents_mean_a_higher_schedule_strength(self):
        games = [norm_game(1, "2025-09-01", 1, 2, None, None, state="scheduled"),
                 norm_game(2, "2025-09-02", 3, 4, None, None, state="scheduled")]
        state = odds.SeasonState(TEAMS, games, "2025-08-01")
        ratings = dict(EQUAL)
        ratings[2], ratings[4] = 1600.0, 1400.0
        extra = odds.schedule_and_projection(state, ratings, 0.0)
        self.assertGreater(extra["sos_remaining"][0], extra["sos_remaining"][2])     # team 1 faces the strong team 2

    def test_teams_with_no_games_left_have_no_schedule_strength(self):
        state = odds.SeasonState(TEAMS, ordered_season(), "2025-12-31")
        extra = odds.schedule_and_projection(state, EQUAL, 24.0)
        self.assertTrue(np.all(np.isnan(extra["sos_remaining"])))


class BacktestTest(unittest.TestCase):
    def setUp(self):
        self.games = complete_season()
        for k, (a, b) in enumerate([(1, 2), (6, 7), (11, 12), (16, 17), (21, 22), (26, 27)]):
            self.games.append(norm_game(50000 + k, "2025-10-05", a, b, 5, 3, season_type="postseason"))
        self.elo = compute.run_elo({2025: self.games}, TEAM_ID_SET)

    def test_season_dates(self):
        dates = odds.season_dates("2025-04-01", "2025-09-28", 7)
        self.assertEqual(dates[0], "2025-04-15")
        self.assertTrue(all(b > a for a, b in zip(dates, dates[1:])))
        self.assertLess(dates[-1], "2025-09-28")

    def test_backtest_produces_scored_records(self):
        outcome = odds.run_backtest(TEAMS, {2025: self.games}, self.elo, [2025], n_sims=100,
                                    step_days=45, seed=1, home_advantage=24.0)
        self.assertTrue(outcome["records"])
        self.assertEqual(len(outcome["records"]) % 30, 0)                 # every team at every date
        self.assertIn("2025", outcome["field_check"])
        self.assertEqual(len(outcome["history"]["2025"]["dates"]),
                         len(outcome["history"]["2025"]["p_playoffs"]["1"]))
        summary = odds.summarize_backtest(outcome["records"])
        self.assertTrue(0 <= summary["overall"]["brier_model"] <= 1)
        self.assertTrue(summary["calibration"])

    def test_summary_scores_a_perfect_forecast_as_zero(self):
        records = [{"season": 2025, "as_of": "2025-06-01", "days_left": 100, "team_id": i,
                    "p": 1.0 if i <= 12 else 0.0, "y": 1.0 if i <= 12 else 0.0,
                    "in_position_now": 1.0 if i <= 12 else 0.0, "p_div": 0.0, "y_div": 0.0}
                   for i in range(1, 31)]
        summary = odds.summarize_backtest(records)
        self.assertEqual(summary["overall"]["brier_model"], 0.0)
        self.assertGreater(summary["overall"]["brier_constant"], 0.2)


class RatingNoiseTest(unittest.TestCase):
    def setUp(self):
        self.state = odds.SeasonState(TEAMS, complete_season(), "2025-01-01")

    def test_default_uses_the_tuned_noise_level(self):
        self.assertEqual(odds.DEFAULT_RATING_NOISE, 30.0)
        a = odds.simulate(self.state, EQUAL, n_sims=300, seed=5)
        b = odds.simulate(self.state, EQUAL, n_sims=300, seed=5, rating_noise=odds.DEFAULT_RATING_NOISE)
        self.assertTrue(np.array_equal(a["p_playoffs"], b["p_playoffs"]))

    def test_zero_noise_turns_the_uncertainty_off(self):
        ratings = dict(EQUAL)
        ratings[1] = 1600.0
        sharp = odds.simulate(self.state, ratings, n_sims=2000, seed=5, rating_noise=0.0)
        default = odds.simulate(self.state, ratings, n_sims=2000, seed=5)
        self.assertGreater(sharp["p_playoffs"][0], default["p_playoffs"][0])   # a 1600 team looks surer with no noise

    def test_equal_teams_stay_equal_and_the_field_size_holds(self):
        result = odds.simulate(self.state, EQUAL, n_sims=2000, seed=6, rating_noise=40.0)
        self.assertAlmostEqual(float(result["p_playoffs"].sum()), 12.0, places=6)
        self.assertTrue(np.all(np.abs(result["p_playoffs"] - 0.4) < 0.05))

    def test_noise_pulls_favorites_down_and_long_shots_up(self):
        ratings = dict(EQUAL)
        ratings[1], ratings[2] = 1600.0, 1400.0
        sharp = odds.simulate(self.state, ratings, n_sims=3000, seed=8, rating_noise=0.0)
        fuzzy = odds.simulate(self.state, ratings, n_sims=3000, seed=8, rating_noise=80.0)
        self.assertLess(fuzzy["p_playoffs"][0], sharp["p_playoffs"][0])       # favorite is less certain
        self.assertGreater(fuzzy["p_playoffs"][1], sharp["p_playoffs"][1])    # long shot is less hopeless

    def test_noise_is_reproducible(self):
        a = odds.simulate(self.state, EQUAL, n_sims=300, seed=9, rating_noise=30.0)
        b = odds.simulate(self.state, EQUAL, n_sims=300, seed=9, rating_noise=30.0)
        self.assertTrue(np.array_equal(a["p_playoffs"], b["p_playoffs"]))

    def test_noise_does_nothing_when_no_games_are_left(self):
        state = odds.SeasonState(TEAMS, ordered_season(), "2025-12-31")
        result = odds.simulate(state, EQUAL, n_sims=10, seed=1, rating_noise=50.0)
        self.assertEqual({state.ids[i] for i in range(30) if result["p_playoffs"][i] == 1.0},
                         {1, 2, 3, 4, 6, 11, 16, 17, 18, 19, 21, 26})


class TuningTest(unittest.TestCase):
    def test_tuning_reports_one_row_per_noise_level(self):
        games = complete_season()
        for k, (a, b) in enumerate([(1, 2), (6, 7), (11, 12), (16, 17), (21, 22), (26, 27)]):
            games.append(norm_game(50000 + k, "2025-10-05", a, b, 5, 3, season_type="postseason"))
        elo = compute.run_elo({2025: games}, TEAM_ID_SET)
        rows = odds.tune_rating_noise(TEAMS, {2025: games}, elo, [2025], [0.0, 25.0], n_sims=60,
                                      step_days=60, seed=1, home_advantage=24.0)
        self.assertEqual([row["rating_noise"] for row in rows], [0.0, 25.0])
        for row in rows:
            self.assertTrue(0 <= row["brier"] <= 1)
            self.assertIn("2025", row["by_season"])
            self.assertIsNotNone(row["calibration_error"])

    def test_calibration_error_is_zero_for_a_perfect_forecast(self):
        records = [{"season": 2025, "as_of": "2025-06-01", "days_left": 100, "team_id": i,
                    "p": 1.0 if i <= 12 else 0.0, "y": 1.0 if i <= 12 else 0.0,
                    "in_position_now": 0.0, "p_div": 0.0, "y_div": 0.0} for i in range(1, 31)]
        self.assertEqual(odds.summarize_backtest(records)["calibration_error"], 0.0)

    def test_calibration_error_detects_overconfidence(self):
        # Always says 90% but only half come true.
        records = [{"season": 2025, "as_of": "2025-06-01", "days_left": 100, "team_id": i,
                    "p": 0.9, "y": 1.0 if i % 2 else 0.0, "in_position_now": 0.0, "p_div": 0.0, "y_div": 0.0}
                   for i in range(1, 31)]
        self.assertAlmostEqual(odds.summarize_backtest(records)["calibration_error"], 0.4, places=3)


class CommandLineTest(unittest.TestCase):
    def write_inputs(self, root: Path):
        raw = root / "raw"
        raw.mkdir()
        games = complete_season()
        games.append(norm_game(90001, "2025-10-05", 1, 2, 5, 3, season_type="postseason"))
        (raw / "teams.json").write_text(json.dumps({"teams": TEAMS}))
        (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": games}))
        self.assertEqual(compute.main(["--raw", str(raw), "--out", str(root / "processed")]), 0)
        return raw, root / "processed"

    def test_single_date_run_writes_odds_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = self.write_inputs(Path(tmp))
            code = odds.main(["--raw", str(raw), "--processed", str(processed),
                              "--as-of", "2025-07-01", "--sims", "300"])
            self.assertEqual(code, 0)
            data = json.loads((processed / "odds.json").read_text())
            self.assertEqual(len(data["teams"]), 30)
            self.assertEqual(data["meta"]["as_of"], "2025-07-01")
            self.assertAlmostEqual(sum(t["p_playoffs"] for t in data["teams"]), 12.0, places=1)

    def test_backtest_run_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = self.write_inputs(Path(tmp))
            code = odds.main(["--raw", str(raw), "--processed", str(processed),
                              "--backtest", "2025", "--sims", "100", "--step-days", "60"])
            self.assertEqual(code, 0)
            self.assertTrue((processed / "backtest.json").exists())
            self.assertTrue((processed / "odds_history.json").exists())

    def test_tune_noise_run_writes_a_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = self.write_inputs(Path(tmp))
            code = odds.main(["--raw", str(raw), "--processed", str(processed),
                              "--tune-noise", "0", "30", "--sims", "60", "--step-days", "60"])
            self.assertEqual(code, 0)
            data = json.loads((processed / "noise_tuning.json").read_text())
            self.assertEqual([row["rating_noise"] for row in data["results"]], [0.0, 30.0])

    def test_rating_noise_flag_is_recorded_in_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = self.write_inputs(Path(tmp))
            odds.main(["--raw", str(raw), "--processed", str(processed), "--as-of", "2025-07-01",
                       "--sims", "100", "--rating-noise", "25"])
            data = json.loads((processed / "odds.json").read_text())
            self.assertEqual(data["meta"]["rating_noise"], 25.0)

    def test_missing_elo_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = self.write_inputs(Path(tmp))
            (processed / "elo.json").unlink()
            self.assertEqual(odds.main(["--raw", str(raw), "--processed", str(processed)]), 2)


if __name__ == "__main__":
    unittest.main()
