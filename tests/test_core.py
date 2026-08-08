"""Core tests: response parsing, game rules, scenario loading, prompts.

    python -m unittest discover tests
"""

import glob
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backends import extract_json, is_valid_response, parse_response
from game import resolve_conventional, simulate, transfer
from scenarios import build_prompt, decision_of, load_scenarios

GOLDEN_DIR = os.path.join(ROOT, "tests", "golden")
SCENARIOS = load_scenarios()


class TestParsing(unittest.TestCase):
    def test_plain_json(self):
        r = parse_response('{"message": "hi", "action_level": 7}')
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["action_level"], 7)
        self.assertIsNone(r["target"])

    def test_fenced_json(self):
        r = parse_response('```json\n{"message": "hi", "action_level": 3}\n```')
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["action_level"], 3)

    def test_json_inside_prose(self):
        raw = ('Let me think about this.\n'
               '{"message": "we act", "action_level": 10, "target": "Beta"}\n'
               'That is my answer.')
        r = parse_response(raw)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["action_level"], 10)

    def test_last_object_wins(self):
        raw = ('{"message": "draft", "action_level": 2}\n'
               '{"message": "final", "action_level": 5}')
        self.assertEqual(parse_response(raw)["action_level"], 5)

    def test_level_as_string(self):
        r = parse_response('{"message": "m", "action_level": "8"}')
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["action_level"], 8)

    def test_parse_failure_carries_no_decision(self):
        r = parse_response("I refuse to answer this.")
        self.assertEqual(r["status"], "parse_failure")
        self.assertIsNone(r["action_level"])

    def test_out_of_range_level_is_failure(self):
        r = parse_response('{"message": "m", "action_level": 42}')
        self.assertEqual(r["status"], "parse_failure")

    def test_timeout_and_error_statuses(self):
        self.assertEqual(parse_response("TIMEOUT")["status"], "timeout")
        self.assertEqual(parse_response("ERROR: boom")["status"], "error")
        self.assertEqual(parse_response("")["status"], "parse_failure")

    def test_extract_json_skips_broken_objects(self):
        raw = '{"broken": \n{"message": "ok", "action_level": 1}'
        self.assertEqual(extract_json(raw)["action_level"], 1)

    def test_extract_json_recovers_object_nested_in_invalid_json(self):
        raw = '{oops {"message": "ok", "action_level": 4} }'
        self.assertEqual(extract_json(raw)["action_level"], 4)

    def test_is_valid_response(self):
        self.assertTrue(is_valid_response({"message": "m", "action_level": 0}))
        self.assertFalse(is_valid_response({"message": "m"}))
        self.assertFalse(is_valid_response({"message": "m", "action_level": "x"}))


class TestGame(unittest.TestCase):
    def test_transfer_pool_first_then_loser(self):
        control = {"Alpha": 0.0, "Beta": 20.0, "pool": 10.0}
        transfer(control, "Alpha", "Beta", 15)
        self.assertEqual(control, {"Alpha": 15.0, "Beta": 15.0, "pool": 0.0})

    def test_transfer_caps_at_available(self):
        control = {"Alpha": 0.0, "Beta": 5.0, "pool": 0.0}
        transfer(control, "Alpha", "Beta", 50)
        self.assertEqual(control, {"Alpha": 5.0, "Beta": 0.0, "pool": 0.0})

    def test_resolve_tie_changes_nothing(self):
        control = {"Alpha": 10.0, "Beta": 10.0, "pool": 80.0}
        resolve_conventional(control, 5, 5)
        self.assertEqual(control, {"Alpha": 10.0, "Beta": 10.0, "pool": 80.0})

    def test_simulate_reproduces_known_controls(self):
        expected = {
            "desperate": {"Alpha": 10.0, "Beta": 55.0, "pool": 35.0},
            "dominant": {"Alpha": 55.0, "Beta": 10.0, "pool": 35.0},
            "balanced": {"Alpha": 30.0, "Beta": 30.0, "pool": 40.0},
        }
        for name, control in expected.items():
            self.assertEqual(simulate(SCENARIOS[name]["history"]), control, name)


class TestScenarios(unittest.TestCase):
    def test_builtin_scenarios_load(self):
        self.assertEqual(sorted(SCENARIOS), ["balanced", "desperate", "dominant"])
        for scenario in SCENARIOS.values():
            self.assertEqual(scenario["langs"],
                             ["en", "fr", "ja", "ko", "pt", "zh"])

    def test_unsupported_language_raises(self):
        with self.assertRaises(ValueError):
            build_prompt(SCENARIOS["dominant"], "de")

    def test_bad_history_length_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "bad.toml"), "w") as f:
                f.write('kind = "escalation"\nhistory = [[4, "Beta", 3, "Alpha"]]\n'
                        '[messages.en]\nalpha = "a"\nbeta = "b"\n')
            with self.assertRaises(ValueError):
                load_scenarios(tmp)

    def test_raw_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "robots.toml"), "w") as f:
                f.write('kind = "raw"\n'
                        '[decision]\nfield = "action_level"\ncatastrophic = 9\n'
                        '[prompt.en]\ntext = """Choose 0-9."""\n')
            robots = load_scenarios(tmp)["robots"]
            self.assertEqual(build_prompt(robots, "en"), "Choose 0-9.")
            self.assertEqual(robots["langs"], ["en"])
            _, catastrophic = decision_of(robots, {"action_level": 9})
            self.assertTrue(catastrophic)
            with self.assertRaises(ValueError):
                build_prompt(robots, "ja")

    def test_decision_of_missing_field(self):
        value, catastrophic = decision_of(SCENARIOS["dominant"],
                                          {"status": "parse_failure"})
        self.assertIsNone(value)
        self.assertFalse(catastrophic)


class TestGoldenPrompts(unittest.TestCase):
    """The refactored prompt builders must reproduce the original prompts
    byte for byte (captured by tests/capture_golden.py before the refactor)."""

    def test_vignette_prompts_unchanged(self):
        for path in glob.glob(os.path.join(GOLDEN_DIR, "vignette_*.txt")):
            name = os.path.basename(path)[len("vignette_"):-len(".txt")]
            rest, lang = name.rsplit("_", 1)
            if "think" in lang:
                lang, reason_lang = lang.split("think")
            else:
                reason_lang = None
            with open(path) as f:
                expected = f.read()
            got = build_prompt(SCENARIOS[rest], lang, reason_lang)
            self.assertEqual(got, expected, name)

    def test_dilemma_and_mad_prompts_unchanged(self):
        from prompts import (make_dilemma_system, make_mad_system,
                             make_retaliation_prompt)
        for lang in ["en", "fr", "ja"]:
            for side, has_nuke in [("alpha", True), ("beta", False)]:
                with open(os.path.join(GOLDEN_DIR,
                                       f"dilemma_{lang}_{side}.txt")) as f:
                    self.assertEqual(
                        make_dilemma_system(lang, side.capitalize(),
                                            has_nuke, 10),
                        f.read(), f"dilemma {lang} {side}")
        for lang in ["en", "ja"]:
            for armed_self, armed_other in [(0, 0), (1, 0), (0, 1), (1, 1)]:
                name = f"mad_{lang}_alpha_{armed_self}{armed_other}.txt"
                with open(os.path.join(GOLDEN_DIR, name)) as f:
                    self.assertEqual(
                        make_mad_system(lang, "Alpha", 5,
                                        bool(armed_self), bool(armed_other)),
                        f.read(), name)
            with open(os.path.join(GOLDEN_DIR, f"retaliation_{lang}.txt")) as f:
                self.assertEqual(make_retaliation_prompt(lang, "Alpha", "Beta"),
                                 f.read())


if __name__ == "__main__":
    unittest.main()
