"""Tests for pipeline/score.py (the Pennant Score)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tests"))

import compute  # noqa: E402
import odds  # noqa: E402
import score  # noqa: E402
from test_pipeline import complete_season, make_teams  # noqa: E402


class PercentileTest(unittest.TestCase):
    def test_ranks_run_from_zero_to_one_hundred(self):
        self.assertEqual(score.percentiles([5, 1, 3]), [100.0, 0.0, 50.0])

    def test_ties_share_the_average_rank(self):
        self.assertEqual(score.percentiles([1, 2, 2, 3]), [0.0, 50.0, 50.0, 100.0])

    def test_missing_values_stay_missing(self):
        self.assertEqual(score.percentiles([None, 2, 1]), [None, 100.0, 0.0])
        self.assertEqual(score.percentiles([None, None]), [None, None])
        self.assertEqual(score.percentiles([7]), [50.0])

    def test_blend_skips_missing_parts_and_reweights(self):
        self.assertEqual(score.blend([(100, 0.3), (None, 0.7)]), 100)
        self.assertAlmostEqual(score.blend([(100, 0.5), (0, 0.5)]), 50)
        self.assertIsNone(score.blend([(None, 1)]))


class ScoreSeasonTest(unittest.TestCase):
    def rows(self):
        return [{"team_id": i, "run_diff_per_game": 2.0 - i * 0.2, "pyth_win_pct": 0.65 - i * 0.02} for i in range(1, 6)]

    def test_the_best_team_scores_highest_without_outlook(self):
        scores = score.score_season(self.rows(), {"1": 1600, "2": 1550, "3": 1500, "4": 1450, "5": 1400})
        self.assertEqual(scores["1"]["score"], 100.0)
        self.assertEqual(scores["5"]["score"], 0.0)
        self.assertIsNone(scores["1"]["outlook"])

    def test_outlook_is_included_when_odds_are_given(self):
        odds_rows = {i: {"p_playoffs": 1 - i * 0.15, "sos_remaining": float(i)} for i in range(1, 6)}
        scores = score.score_season(self.rows(), {str(i): 1600 - i * 50 for i in range(1, 6)}, odds_rows)
        self.assertEqual(scores["1"]["outlook"], 100.0)
        self.assertEqual(scores["5"]["outlook"], 0.0)

    def test_weights_are_thirty_forty_five_twenty_five(self):
        self.assertEqual(score.WEIGHTS, {"results": 0.30, "underlying": 0.45, "outlook": 0.25})
        self.assertAlmostEqual(sum(score.WEIGHTS.values()), 1.0)


class CommandLineTest(unittest.TestCase):
    def test_main_writes_scores_for_every_season(self):
        import test_pipeline as tp
        teams = make_teams()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            games = complete_season()
            games.append(tp.norm_game(90001, "2025-10-05", 1, 2, 5, 3, season_type="postseason"))
            (raw / "teams.json").write_text(json.dumps({"teams": teams}))
            (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": games}))
            self.assertEqual(compute.main(["--raw", str(raw), "--out", str(root / "processed")]), 0)
            self.assertEqual(odds.main(["--raw", str(raw), "--processed", str(root / "processed"),
                                        "--as-of", "2025-07-01", "--sims", "100"]), 0)
            self.assertEqual(score.main(["--processed", str(root / "processed")]), 0)
            data = json.loads((root / "processed" / "scores.json").read_text())
            self.assertEqual(len(data["seasons"]["2025"]), 30)
            self.assertTrue(all(0 <= v["score"] <= 100 for v in data["seasons"]["2025"].values()))
            self.assertIsNotNone(data["seasons"]["2025"]["1"]["score"])

    def test_missing_input_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(score.main(["--processed", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
