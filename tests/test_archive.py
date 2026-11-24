"""Tests for pipeline/archive.py (the scoreboard archive)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tests"))

import archive  # noqa: E402
import compute  # noqa: E402
from test_compute import line_game  # noqa: E402
from test_pipeline import complete_season, make_teams  # noqa: E402

TEAMS = make_teams()
IDS = {t["id"] for t in TEAMS}


def short_season():
    """The first eight game days of a season, plus one walk-off game on a ninth."""
    games = [g for g in complete_season() if g["date_et"] <= "2025-04-03"]
    games.append(line_game(888001, 1, 2, [0, 0, 0, 0, 0, 0, 2, 0, 3], [1, 0, 0, 0, 0, 0, 3, 0, 0], date="2025-04-04"))
    return games


def inputs(tmp):
    root = Path(tmp)
    raw = root / "raw"
    raw.mkdir()
    (raw / "teams.json").write_text(json.dumps({"teams": TEAMS}))
    (raw / "games_2025.json").write_text(json.dumps({"meta": {}, "games": short_season()}))
    assert compute.main(["--raw", str(raw), "--out", str(root / "processed")]) == 0
    return raw, root / "processed"


class BuildSeasonTest(unittest.TestCase):
    def setUp(self):
        self.games = short_season()
        self.elo = compute.run_elo({2025: self.games}, IDS)
        self.args = (TEAMS, self.games, self.elo, 2025, 60, 1, 24.0, 30.0)

    def test_one_entry_per_game_day(self):
        out, added = archive.build_season(*self.args)
        days = sorted({g["date_et"] for g in self.games})
        self.assertEqual(sorted(out["days"]), days)
        self.assertEqual(added, len(days))
        self.assertEqual(out["meta"]["ids"], sorted(IDS))

    def test_game_rows_and_tags(self):
        out, _ = archive.build_season(*self.args)
        day = out["days"]["2025-04-04"]
        self.assertEqual(len(day["g"]), 1)
        away, home, away_runs, home_runs, innings, p_winner, tags = day["g"][0]
        self.assertEqual((away, home, away_runs, home_runs), (2, 1, 4, 5))
        self.assertIn("walkoff", tags)
        self.assertIn("comeback", tags)
        self.assertTrue(0 < p_winner < 1000)
        total = sum(len(d["g"]) for d in out["days"].values())
        self.assertEqual(total, len([g for g in self.games if g["status_state"] == "final"]))

    def test_odds_arrays_cover_every_team_and_add_up(self):
        out, _ = archive.build_season(*self.args)
        for day in out["days"].values():
            self.assertEqual((len(day["po"]), len(day["dv"])), (30, 30))
            self.assertTrue(all(0 <= x <= 1000 for x in day["po"]))
            self.assertAlmostEqual(sum(day["po"]) / 1000, 12.0, delta=0.3)      # rounding to thousandths
        self.assertEqual(len(out["meta"]["pre"]), 30)

    def test_second_run_adds_nothing_and_rebuild_recomputes(self):
        first, _ = archive.build_season(*self.args)
        again, added = archive.build_season(*self.args, existing=json.loads(json.dumps(first)))
        self.assertEqual(added, 0)
        self.assertEqual(again["days"], first["days"])

    def test_new_days_are_appended_to_an_existing_file(self):
        partial = [g for g in self.games if g["date_et"] <= "2025-04-02"]
        old, _ = archive.build_season(TEAMS, partial, self.elo, 2025, 60, 1, 24.0, 30.0)
        saved = json.loads(json.dumps(old["days"]))
        full, added = archive.build_season(*self.args, existing=old)
        self.assertEqual(added, 2)                                   # April 3 and April 4
        self.assertEqual(len(full["days"]), len(saved) + 2)
        self.assertTrue(all(full["days"][day] == saved[day] for day in saved))     # earlier days untouched

    def test_season_with_no_games_returns_nothing(self):
        out, added = archive.build_season(TEAMS, [], self.elo, 2025, 60, 1, 24.0, 30.0)
        self.assertEqual((out, added), (None, 0))


class CommandLineTest(unittest.TestCase):
    def test_main_writes_archive_and_skips_saved_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, processed = inputs(tmp)
            self.assertEqual(archive.main(["--raw", str(raw), "--processed", str(processed), "--seasons", "2025", "--sims", "40"]), 0)
            path = processed / "archive_2025.json"
            data = json.loads(path.read_text())
            self.assertIn("2025-04-04", data["days"])
            stamp = path.read_text()
            self.assertEqual(archive.main(["--raw", str(raw), "--processed", str(processed), "--seasons", "2025", "--sims", "40"]), 0)
            self.assertEqual(json.loads(path.read_text())["days"], data["days"])
            self.assertEqual(archive.main(["--raw", str(raw), "--processed", str(processed), "--seasons", "1999"]), 0)   # not in the data

    def test_missing_inputs_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(archive.main(["--raw", str(Path(tmp) / "none"), "--processed", tmp]), 2)


if __name__ == "__main__":
    unittest.main()
