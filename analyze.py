"""Aggregate launch rates from vignette runs.

Reads every run record under data/vignette/ and prints launch rates by
model, scenario, and language. Failed runs (parse failure, timeout, error)
are excluded from the rates and counted in their own column.
"""

import argparse
import collections
import glob
import json
import os

# Per-batch summary files, not run records.
SUMMARY_FILES = {"vignette_results.json", "crosslang_results.json"}


def load_records(data_dir: str) -> list[dict]:
    records = []
    pattern = os.path.join(data_dir, "vignette", "**", "*.json")
    for path in glob.glob(pattern, recursive=True):
        if os.path.basename(path) in SUMMARY_FILES:
            continue
        try:
            with open(path) as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(record, dict) and "scenario" in record and "model" in record:
            records.append(record)
    return records


def run_failed(record: dict) -> bool:
    """True if the run carries no usable decision.

    New records have an explicit status. Records written before the status
    field are failures when their reasoning holds the old PARSE_FAILURE
    marker or the run timed out.
    """
    if "status" in record:
        return record["status"] != "ok"
    return (record.get("reasoning") == "PARSE_FAILURE"
            or record.get("timed_out", False)
            or record.get("action_level") is None)


def aggregate(records: list[dict]) -> dict:
    """Group records into (model, scenario, lang) -> [launches, valid, failed]."""
    counts = collections.defaultdict(lambda: [0, 0, 0])
    for record in records:
        lang = record.get("lang", "?")
        if record.get("reason_lang"):
            lang += f">{record['reason_lang']}"
        key = (record["model"], record["scenario"], lang)
        if run_failed(record):
            counts[key][2] += 1
            continue
        counts[key][1] += 1
        # New records say which decision was the catastrophic one; records
        # written before that field always used level 10.
        if record.get("catastrophic", record.get("action_level") == 10):
            counts[key][0] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch rates by model, scenario, and language.")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--model", help="only this model")
    parser.add_argument("--scenario", help="only this scenario")
    parser.add_argument("--lang", help="only this prompt language")
    args = parser.parse_args()

    counts = aggregate(load_records(args.data_dir))
    rows = [
        (model, scenario, lang, launches, valid, failed)
        for (model, scenario, lang), (launches, valid, failed)
        in sorted(counts.items())
        if (not args.model or model == args.model)
        and (not args.scenario or scenario == args.scenario)
        and (not args.lang or lang.startswith(args.lang))
    ]
    if not rows:
        print("no matching runs")
        return

    print(f"{'model':<22} {'scenario':<10} {'lang':<7} "
          f"{'launch':>6} {'runs':>5} {'rate':>6} {'failed':>7}")
    for model, scenario, lang, launches, valid, failed in rows:
        rate = f"{100 * launches / valid:>5.0f}%" if valid else "    --"
        print(f"{model:<22} {scenario:<10} {lang:<7} "
              f"{launches:>6} {valid:>5} {rate} {failed:>7}")


if __name__ == "__main__":
    main()
