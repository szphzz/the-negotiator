"""
Builds the final ranked comparison report (schemas/comparison_report.schema.json) from
a set of gathered quotes - the output of The Caller (and, where negotiated, The
Closer). This is the "reports back with a ranked, evidence-backed comparison" step:
pure data logic, no LLM call needed once the quotes are in structured form.

Usage:
    python build_comparison_report.py quotes_input.json --job-spec ../../schemas/examples/valid_spec_example.json

quotes_input.json shape - one entry per company called:
[
  {
    "quote_id": "carolina",
    "company_name": "Carolina Movers Co.",
    "company_phone": "555-0101",
    "call_outcome_type": "itemized_quote",
    "original_total": 2400,
    "negotiated_total": 2200,
    "negotiation_outcome_type": "price_matched",
    "binding": true,
    "fees": [{"name": "fuel surcharge", "amount": 150}],
    "concessions": [{"item": "deposit", "before": "20% non-refundable", "after": "10%, refundable"}],
    "deposit_required": 240,
    "deposit_refundable": true,
    "cancellation_policy": "48 hours notice",
    "transcript_refs": ["caller/transcripts/tough_20260718_205830/transcript.txt",
                         "closer/transcripts/tough_20260718_213052/transcript.txt"]
  },
  ...
]

Only entries with call_outcome_type == "itemized_quote" are ranked and eligible for
recommendation - callback_commitment and documented_decline entries are carried
through in the report but excluded from ranking (there is no price to rank).

Install dependency for schema validation (optional but recommended):
    pip install jsonschema --break-system-packages
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent.parent / "schemas" / "comparison_report.schema.json"
BENCHMARKS_PATH = Path(__file__).parent.parent / "config" / "market_benchmarks.json"

RANKABLE_OUTCOME = "itemized_quote"


def load_benchmarks() -> dict:
    return json.loads(BENCHMARKS_PATH.read_text())


def compute_red_flags(quotes: list, benchmarks: dict) -> None:
    """Mutates each rankable quote in place, setting/overwriting red_flag based on the
    config-driven rule: session comparison against the gathered median when there are
    enough quotes, otherwise the static benchmark fallback."""
    rule = benchmarks["red_flag_rule"]
    rankable = [q for q in quotes if q.get("call_outcome_type") == RANKABLE_OUTCOME]
    totals = [q["total_estimate"] for q in rankable]

    if len(totals) >= 3:
        threshold_pct = rule["session_comparison"]["threshold_pct_below_median"]
        median = statistics.median(totals)
        basis = f"session median (${median:,.0f}) across {len(totals)} gathered quotes"
    else:
        threshold_pct = rule["static_benchmark_fallback"]["threshold_pct_below_reference_median"]
        median = rule["static_benchmark_fallback"]["reference_range_usd"]["median_estimate"]
        basis = f"static benchmark median (${median:,.0f}) - fewer than 3 quotes gathered this session"

    cutoff = median * (1 - threshold_pct / 100)

    for q in rankable:
        existing = q.get("red_flag") or {}
        if existing.get("flagged"):
            # Preserve a flag + resolution already set by the Closer's callback probe.
            continue
        if q["total_estimate"] < cutoff:
            q["red_flag"] = {
                "flagged": True,
                "reason": f"${q['total_estimate']:,.0f} is more than {threshold_pct}% below {basis} (cutoff: ${cutoff:,.0f})",
                "resolution": existing.get("resolution"),
            }
        else:
            q.setdefault("red_flag", {"flagged": False, "reason": None, "resolution": None})


def rank_quotes(quotes: list) -> None:
    """Assigns `rank` to rankable quotes: lower total_estimate wins, binding beats
    non-binding at a tie, unflagged beats flagged at a tie."""
    rankable = [q for q in quotes if q.get("call_outcome_type") == RANKABLE_OUTCOME]
    rankable.sort(key=lambda q: (
        q["total_estimate"],
        0 if q.get("binding") else 1,
        1 if q.get("red_flag", {}).get("flagged") else 0,
    ))
    for i, q in enumerate(rankable, start=1):
        q["rank"] = i


def pick_recommendation(quotes: list) -> tuple[str, str]:
    rankable = [q for q in quotes if q.get("call_outcome_type") == RANKABLE_OUTCOME]
    if not rankable:
        return None, "No itemized quotes were gathered - only callbacks/declines on file, nothing to rank."

    unflagged = [q for q in rankable if not q.get("red_flag", {}).get("flagged")]
    pool = unflagged if unflagged else rankable
    best = min(pool, key=lambda q: q["total_estimate"])

    reasons = [f"{best['company_name']} at ${best['total_estimate']:,.0f}"]
    reasons.append("binding" if best.get("binding") else "non-binding - confirm before booking")
    if best.get("concessions"):
        reasons.append(f"includes concessions won via negotiation: {best['concessions']}")

    flagged_others = [q for q in rankable if q is not best and q.get("red_flag", {}).get("flagged")]
    if flagged_others:
        cheapest_flagged = min(flagged_others, key=lambda q: q["total_estimate"])
        reasons.append(
            f"{cheapest_flagged['company_name']}'s ${cheapest_flagged['total_estimate']:,.0f} was cheaper but "
            f"flagged as a red flag ({cheapest_flagged['red_flag']['reason']}) and was excluded from the top pick"
        )

    explanation = f"Recommended: {reasons[0]} ({reasons[1]})."
    if len(reasons) > 2:
        explanation += " " + " ".join(reasons[2:]) + "."

    return best["quote_id"], explanation


def build_report(quotes_input: list, job_spec: dict) -> dict:
    benchmarks = load_benchmarks()

    quotes = json.loads(json.dumps(quotes_input))  # deep copy
    for q in quotes:
        if q.get("call_outcome_type") == RANKABLE_OUTCOME:
            q["total_estimate"] = q.get("negotiated_total") or q["original_total"]
        else:
            q.setdefault("total_estimate", None)
            q.setdefault("binding", None)
            q.setdefault("fees", [])
            q.setdefault("red_flag", {"flagged": False, "reason": None, "resolution": None})
            q.setdefault("rank", None)

    compute_red_flags(quotes, benchmarks)
    rank_quotes(quotes)
    recommended_id, explanation = pick_recommendation(quotes)

    return {
        "job_spec_version_hash": job_spec.get("spec_version_hash"),
        "quotes": quotes,
        "recommended_quote_id": recommended_id,
        "recommendation_explanation": explanation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_report(report: dict) -> list:
    try:
        from jsonschema import Draft7Validator
    except ImportError:
        print("(jsonschema not installed - skipping schema validation. "
              "pip install jsonschema --break-system-packages to enable it.)")
        return []
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft7Validator(schema)
    return sorted(validator.iter_errors(report), key=lambda e: e.path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("quotes_input", help="Path to a JSON file with gathered quotes (see module docstring for shape)")
    parser.add_argument("--job-spec", required=True, help="Path to the confirmed job spec used across all calls")
    parser.add_argument("--out", default=None, help="Where to write the report (default: prints to stdout)")
    args = parser.parse_args()

    quotes_input = json.loads(Path(args.quotes_input).read_text())
    job_spec = json.loads(Path(args.job_spec).read_text())

    report = build_report(quotes_input, job_spec)
    errors = validate_report(report)
    if errors:
        print(f"SCHEMA WARNINGS ({len(errors)}):")
        for err in errors:
            field_path = ".".join(str(p) for p in err.path) or "(root)"
            print(f"  - {field_path}: {err.message}")

    output = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(output)
        print(f"\nReport written to {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
