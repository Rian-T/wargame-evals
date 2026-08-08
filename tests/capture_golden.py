"""Capture the current prompts as golden files.

The golden files freeze the exact prompts sent to the models; the test
suite requires the code to keep reproducing them byte for byte. Rerun this
script only to deliberately re-freeze after an intentional prompt change:

    python tests/capture_golden.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from prompts import make_dilemma_system, make_mad_system, make_retaliation_prompt
from scenarios import build_prompt as build_scenario_prompt
from scenarios import load_scenarios

SCENARIOS = load_scenarios()
GOLDEN_DIR = os.path.join(ROOT, "tests", "golden")


def build_prompt(scenario_name: str, lang: str, reason_lang: str | None = None) -> str:
    return build_scenario_prompt(SCENARIOS[scenario_name], lang, reason_lang)


def write(name: str, text: str) -> None:
    path = os.path.join(GOLDEN_DIR, name)
    with open(path, "w") as f:
        f.write(text)
    print(f"  {name} ({len(text)} chars)")


def main() -> None:
    os.makedirs(GOLDEN_DIR, exist_ok=True)

    # Vignette: every scenario x language, plus the crosslang variants.
    for scenario in SCENARIOS:
        for lang in SCENARIOS[scenario]["langs"]:
            write(f"vignette_{scenario}_{lang}.txt", build_prompt(scenario, lang))
    for lang, reason_lang in [("en", "ja"), ("ja", "en")]:
        write(f"vignette_dominant_{lang}think{reason_lang}.txt",
              build_prompt("dominant", lang, reason_lang))

    # Dilemma: both sides, every language the experiment supports.
    for lang in ["en", "fr", "ja"]:
        write(f"dilemma_{lang}_alpha.txt",
              make_dilemma_system(lang, "Alpha", True, 10))
        write(f"dilemma_{lang}_beta.txt",
              make_dilemma_system(lang, "Beta", False, 10))

    # MAD: every armed state, both languages, plus retaliation.
    for lang in ["en", "ja"]:
        for armed_self, armed_other in [(False, False), (True, False),
                                        (False, True), (True, True)]:
            write(f"mad_{lang}_alpha_{int(armed_self)}{int(armed_other)}.txt",
                  make_mad_system(lang, "Alpha", 5, armed_self, armed_other))
        write(f"retaliation_{lang}.txt",
              make_retaliation_prompt(lang, "Alpha", "Beta"))


if __name__ == "__main__":
    main()
