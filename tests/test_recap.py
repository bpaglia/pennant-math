"""Tests for pipeline/recap.py (the daily recap)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tests"))

import compute  # noqa: E402
import recap  # noqa: E402
from test_compute import line_game  # noqa: E402
from test_pipeline import complete_season, make_teams, norm_game  # noqa: E402

TEAMS = make_teams()
IDS = {t["id"] for t in TEAMS}


def full_inputs(tmp):
    root = Path(tmp)
    raw = root / "raw"
    raw.mkdir()
    games = complete_season()
    games.append(line_game(777001, 1, 2, [0, 0, 0, 0, 0, 0, 2, 0, 3], [1, 0, 0, 0, 0, 0, 3, 0, 0], date="2025-09-05"))   # walk-off + comeback
    games.append(norm_game(777002, "2025-10-05", 1, 2, 5, 3, season_type="postseason"))
    (raw / "teams.json").write_text(json.dumps({"teams": TEAMS}))
    (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": games}))
    assert compute.main(["--raw", str(raw), "--out", str(root / "processed")]) == 0
    return raw, root / "processed", games


class LineFactsTest(unittest.TestCase):
    def test_walk_off_and_comeback(self):
        game = line_game(1, 1, 2, [0, 0, 0, 0, 0, 0, 2, 0, 3], [1, 0, 0, 0, 0, 0, 3, 0, 0])
        self.assertEqual(recap.line_facts(game), {"walkoff": True, "comeback": True})

    def test_no_walk_off_when_home_did_not_bat_last(self):
        game = line_game(1, 1, 2, [0, 3, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 1, 0])
        self.assertEqual(recap.line_facts(game), {"walkoff": False, "comeback": False})

    def test_road_team_comeback(self):
        game = line_game(1, 1, 2, [2, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 3, 0])
        self.assertTrue(recap.line_facts(game)["comeback"])

    def test_missing_or_wrong_lines_give_no_facts(self):
        self.assertEqual(recap.line_facts(norm_game(1, "2025-06-01", 1, 2, 5, 3)), {})
        bad = line_game(1, 1, 2, [0, 0, 0, 0, 0, 0, 0, 0, 3], [1, 0, 0, 0, 0, 0, 0, 0, 0])
        bad["home_runs"] = 4
        self.assertEqual(recap.line_facts(bad), {})

    def test_definitions_match_the_season_totals_in_compute(self):
        games = [line_game(1, 1, 2, [0, 0, 0, 0, 0, 0, 2, 0, 3], [1, 0, 0, 0, 0, 0, 3, 0, 0]),
                 line_game(2, 3, 4, [0, 3, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 1, 0]),
                 line_game(3, 5, 6, [1, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 3, 0])]
        standings = compute.compute_standings(games, TEAMS)["teams"]
        self.assertEqual(sum(1 for g in games if recap.line_facts(g)["walkoff"]), sum(r["walkoff_wins"] for r in standings))
        self.assertEqual(sum(1 for g in games if recap.line_facts(g)["comeback"]), sum(r["comeback_wins"] for r in standings))


class TagTest(unittest.TestCase):
    def test_tags(self):
        game = norm_game(1, "2025-06-01", 1, 2, 7, 0)
        self.assertEqual(recap.game_tags(game, 0.30), ["upset", "blowout", "shutout"])
        self.assertEqual(recap.game_tags(norm_game(1, "2025-06-01", 1, 2, 4, 3), 0.60), ["one_run"])
        extra = norm_game(1, "2025-06-01", 1, 2, 4, 3)
        extra["innings"] = 11
        self.assertIn("extras", recap.game_tags(extra, 0.6))
        self.assertIn("postseason", recap.game_tags(norm_game(1, "2025-10-01", 1, 2, 4, 2, season_type="postseason"), 0.6))
        self.assertNotIn("upset", recap.game_tags(norm_game(1, "2025-06-01", 1, 2, 4, 2), 0.40))     # not below 40%


class BuildRecapTest(unittest.TestCase):
    def test_recap_for_a_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, games = full_inputs(tmp)
            elo = json.loads((processed / "elo.json").read_text())
            out = recap.build_recap(TEAMS, games, elo, 2025, "2025-09-05", 500, 1, 24.0, 30.0)
            todays = [g for g in games if g["date_et"] == "2025-09-05" and g["status_state"] == "final"]
            self.assertEqual(len(out["games"]), len(todays))
            self.assertEqual(out["summary"]["games"], len(todays))
            self.assertGreaterEqual(out["summary"]["walkoffs"], 1)
            self.assertTrue(all(0 < g["p_winner"] < 1 for g in out["games"]))
            self.assertEqual(len(out["odds"]), 30)
            self.assertAlmostEqual(sum(o["after"] for o in out["odds"].values()), 12.0, places=1)
            ups = [m["after"] - m["before"] for m in out["movers"]["up"]]
            self.assertEqual(ups, sorted(ups, reverse=True))
            self.assertTrue(all(u >= recap.MOVER_MIN for u in ups))
            self.assertTrue(all(m["after"] - m["before"] <= -recap.MOVER_MIN for m in out["movers"]["down"]))

    def test_biggest_upset_is_the_least_likely_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, games = full_inputs(tmp)
            out = recap.build_recap(TEAMS, games, json.loads((processed / "elo.json").read_text()), 2025, "2025-07-01", 300, 1, 24.0, 30.0)
            upsets = [g for g in out["games"] if "upset" in g["tags"]]
            if upsets:
                self.assertEqual(out["summary"]["biggest_upset"]["p_winner"], min(g["p_winner"] for g in upsets))
            else:
                self.assertIsNone(out["summary"]["biggest_upset"])

    def test_latest_game_day_includes_the_postseason(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, games = full_inputs(tmp)
            self.assertEqual(recap.latest_game_day({2025: games}, IDS), (2025, "2025-10-05"))


class CommandLineTest(unittest.TestCase):
    def test_main_writes_recap_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, _ = full_inputs(tmp)
            self.assertEqual(recap.main(["--raw", str(raw), "--processed", str(processed), "--sims", "200"]), 0)
            data = json.loads((processed / "recap.json").read_text())
            self.assertEqual(data["meta"]["date"], "2025-10-05")
            self.assertTrue(data["meta"]["season_complete"])

    def test_date_option_and_missing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed, _ = full_inputs(tmp)
            self.assertEqual(recap.main(["--raw", str(raw), "--processed", str(processed), "--sims", "100", "--date", "2025-09-05"]), 0)
            self.assertEqual(json.loads((processed / "recap.json").read_text())["meta"]["date"], "2025-09-05")
            self.assertEqual(recap.main(["--raw", str(raw), "--processed", str(processed), "--date", "2025-01-01"]), 2)

    def test_missing_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(recap.main(["--raw", str(Path(tmp) / "none"), "--processed", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
