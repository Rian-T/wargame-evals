"""Vignette experiment: a fixed situation, the model makes one decision.

Each run is a single model call. The only variable across conditions is the
language of the prompt. Scenarios are TOML files in scenarios/.

    python vignette.py --model sonnet --scenario dominant --lang en --seeds 30
    python vignette.py --model sonnet --crosslang --seeds 30
"""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from backends import ALL_MODELS, CLI_MODELS, call_model
from scenarios import build_prompt, decision_of, load_scenarios

SCENARIOS = load_scenarios()

# Command-line models can reason for a long time, especially in Japanese, so
# their timeout is set high enough that no run is lost to a cutoff.
CLI_TIMEOUT_S = 1800
API_TIMEOUT_S = 300

# Truncation applied to a raw reply kept for inspection after a parse failure.
MAX_SAVED_RAW_CHARS = 3000
PROGRESS_EVERY = 10

# Cross-language conditions: (prompt language, reasoning language or None).
CROSSLANG_CONDITIONS = [("en", None), ("en", "ja"), ("ja", "en"), ("ja", None)]


def make_run_id(scenario: str, model: str, lang: str, seed: int,
                reason_lang: str | None = None) -> str:
    think = f"think{reason_lang}" if reason_lang else ""
    safe_model = model.replace("/", "_")  # OpenRouter slugs are not paths
    return f"{scenario}_{safe_model}_{lang}{think}_seed{seed:02d}"


def run_vignette(scenario_name: str, lang: str, seed: int, model: str,
                 output_dir: str, reason_lang: str | None = None,
                 timeout: int | None = None) -> dict:
    """Run one vignette and write its result to disk."""
    scenario = SCENARIOS[scenario_name]
    prompt = build_prompt(scenario, lang, reason_lang)
    if timeout is None:
        timeout = CLI_TIMEOUT_S if model in CLI_MODELS else API_TIMEOUT_S
    started = time.time()
    result = call_model(prompt, model, timeout=timeout)
    elapsed_s = round(time.time() - started, 1)
    decision, catastrophic = decision_of(scenario, result)
    run_id = make_run_id(scenario_name, model, lang, seed, reason_lang)

    record = {k: v for k, v in result.items() if k != "raw"}
    if result["status"] != "ok":
        record["raw"] = (result.get("raw") or "")[:MAX_SAVED_RAW_CHARS]
    record.update(scenario=scenario_name, lang=lang, seed=seed, model=model,
                  elapsed_s=elapsed_s, catastrophic=catastrophic)
    if reason_lang:
        record["reason_lang"] = reason_lang

    with open(os.path.join(output_dir, f"{run_id}.json"), "w") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

    shown = decision if result["status"] == "ok" else result["status"]
    print(f"  {run_id}: level={shown} ({elapsed_s}s)"
          f"{' LAUNCH' if catastrophic else ''}")
    return {"run_id": run_id, "scenario": scenario_name, "lang": lang,
            "seed": seed, "model": model, "reason_lang": reason_lang,
            "status": result["status"], "action_level": decision,
            "nuked": catastrophic}


def run_all(tasks: list, model: str, output_dir: str, max_workers: int,
            timeout: int | None = None) -> list:
    """Run tasks of (scenario, lang, seed, reason_lang) in parallel.

    A run whose output file already exists is skipped, so an interrupted
    batch can be relaunched on the same output directory. A run that raises
    is recorded with status "error" so the batch completes.
    """
    os.makedirs(output_dir, exist_ok=True)
    todo = []
    for scenario, lang, seed, reason_lang in tasks:
        run_id = make_run_id(scenario, model, lang, seed, reason_lang)
        if os.path.exists(os.path.join(output_dir, f"{run_id}.json")):
            continue
        todo.append((scenario, lang, seed, reason_lang))
    if len(todo) < len(tasks):
        print(f"  resuming: {len(tasks) - len(todo)} runs already done")

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(run_vignette, scenario, lang, seed, model,
                            output_dir, reason_lang, timeout):
            (scenario, lang, seed, reason_lang)
            for scenario, lang, seed, reason_lang in todo}
        for done, future in enumerate(as_completed(futures), 1):
            scenario, lang, seed, reason_lang = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                print(f"  ERROR {scenario}/{lang}/{seed}: {e}")
                results.append({"run_id": make_run_id(scenario, model, lang,
                                                      seed, reason_lang),
                                "scenario": scenario, "lang": lang, "seed": seed,
                                "model": model, "reason_lang": reason_lang,
                                "status": "error", "action_level": None,
                                "nuked": False, "error": str(e)})
            if done % PROGRESS_EVERY == 0:
                print(f"  --- {done}/{len(todo)} done ---")
    return results


def summarize(results: list, label) -> None:
    """Print the launch rate and mean pressure level of each group of runs.

    Failed runs (parse failure, timeout, error) are excluded from the rates
    and reported separately.
    """
    groups = {}
    for run in results:
        groups.setdefault(label(run), []).append(run)
    for name, runs in groups.items():
        ok = [r for r in runs if r.get("status") == "ok"]
        failed = len(runs) - len(ok)
        launches = sum(1 for r in ok if r["nuked"])
        levels = [r["action_level"] for r in ok
                  if isinstance(r["action_level"], (int, float))]
        mean = sum(levels) / len(levels) if levels else 0
        rate = launches / len(ok) * 100 if ok else 0
        line = (f"  {name}: {launches}/{len(ok)} launched ({rate:.0f}%) "
                f"| mean level={mean:.1f}")
        if failed:
            line += f" | {failed} failed run{'s' if failed > 1 else ''} excluded"
        print(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=list(SCENARIOS) + ["all"],
                        default="all")
    parser.add_argument("--lang", default="all",
                        help="language code, comma-separated list, or 'all' "
                             "(every language the scenario defines)")
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--model", required=True,
                        help="a name from this list, or any OpenRouter slug "
                             "(vendor/model): " + ", ".join(ALL_MODELS))
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--timeout", type=int,
                        help="seconds per model call (default: 300 for APIs, "
                             "1800 for CLI tools)")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--resume", metavar="DIR",
                        help="continue an interrupted batch in this directory")
    parser.add_argument("--crosslang", action="store_true",
                        help="cross-language reasoning test: "
                             "EN/JA prompt x EN/JA reasoning")
    args = parser.parse_args()

    if args.model not in ALL_MODELS and "/" not in args.model:
        parser.error(f"unknown model {args.model!r}: use a listed name or an "
                     f"OpenRouter slug (vendor/model)")

    if args.resume:
        output_dir = args.resume
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "crosslang_" if args.crosslang else ""
        output_dir = os.path.join(args.output_dir, "vignette", prefix + timestamp)

    if args.crosslang:
        scenario = "dominant" if args.scenario == "all" else args.scenario
        missing = {"en", "ja"} - set(SCENARIOS[scenario]["langs"])
        if missing:
            parser.error(f"--crosslang needs en and ja; scenario {scenario!r} "
                         f"lacks {', '.join(sorted(missing))}")
        tasks = [(scenario, lang, seed, reason_lang)
                 for lang, reason_lang in CROSSLANG_CONDITIONS
                 for seed in range(1, args.seeds + 1)]
        results_file = "crosslang_results.json"

        def label(run):
            return f"{run['lang'].upper()}->{(run['reason_lang'] or run['lang']).upper()}"
    else:
        names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
        tasks = []
        for name in names:
            langs = (SCENARIOS[name]["langs"] if args.lang == "all"
                     else args.lang.split(","))
            for lang in langs:
                if lang not in SCENARIOS[name]["langs"]:
                    parser.error(f"scenario {name!r} does not define language "
                                 f"{lang!r} (available: "
                                 f"{', '.join(SCENARIOS[name]['langs'])})")
                tasks += [(name, lang, seed, None)
                          for seed in range(1, args.seeds + 1)]
        results_file = "vignette_results.json"

        def label(run):
            return f"{run['scenario']} {run['lang'].upper()}"

    print(f"\n{len(tasks)} runs | model: {args.model} | output: {output_dir}\n")
    results = run_all(tasks, args.model, output_dir, args.max_workers,
                      args.timeout)

    with open(os.path.join(output_dir, results_file), "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {output_dir}\n")
    summarize(results, label)


if __name__ == "__main__":
    main()
