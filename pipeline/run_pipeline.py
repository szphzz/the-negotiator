"""
End-to-end pipeline driver for The Negotiator.

Chains the three modules that are today only tested in isolation - Estimator, Caller,
Closer - into one automated run, plus the final ranked report:

  1. ESTIMATOR - seed a voice-interview conversation with estimator/initial_job_spec.json
     as the document-intake starting context (per
     estimator/document_intake_extraction.md's merge behavior), run it against a
     simulated customer persona, and extract + validate the confirmed job spec.
  2. CALLER - call each fixed simulated company (Carolina/tough, Budget/lowballer,
     Premier/upseller, Reliable/stonewaller - see caller/counterparty_personas.md)
     against the confirmed spec, extracting a structured outcome from each.
  3. CLOSER - run one full negotiation round (closer/closer_orchestration.md) over
     every itemized quote gathered in step 2, ratcheting current_best_offer call by
     call and excluding red-flagged quotes from being cited as leverage, per
     closer/config/market_benchmarks.json. QuickHaul Express, a $950 outlier never
     called by The Caller (see closer/negotiation_scenarios.md scenario 4), is
     optionally injected as a red-flag stress test.
  4. REPORT - build the final ranked comparison_report.schema.json from every
     gathered quote (negotiated where applicable, carried through as callback/decline
     otherwise), with transcript_refs pointing at the real transcripts each stage saved.

This reuses each module's existing eval-harness building blocks (system prompts +
simulated personas + prompt loaders + JSON extraction) rather than forking them - the
only new logic here is the sequencing and the plumbing between stages.

Setup (same as every module's run_*_eval_openai.py):
    pip install openai jsonschema --break-system-packages
    export OPENAI_API_KEY=your_key_here

Usage:
    python pipeline/run_pipeline.py
    python pipeline/run_pipeline.py --customer-persona vague --show-transcript
    python pipeline/run_pipeline.py --companies carolina,budget --no-redflag
    python pipeline/run_pipeline.py --turns-estimator 20 --turns-caller 10 --turns-closer 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
for sub in ("estimator/scripts", "caller/scripts", "closer/scripts"):
    sys.path.insert(0, str(ROOT / sub))

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency. Install with:\n    pip install openai --break-system-packages")

import run_estimator_eval_openai as estimator_eval
import run_caller_eval_openai as caller_eval
import run_closer_eval_openai as closer_eval
import validate_job_spec as validator
import plan_negotiation_round as planner
import build_comparison_report as reporter

INITIAL_SPEC_PATH = ROOT / "estimator" / "initial_job_spec.json"
PIPELINE_RUNS_DIR = ROOT / "pipeline" / "runs"

# Simulated customers for the Estimator stage, tailored to initial_job_spec.json's
# actual addresses (Sacramento -> Los Angeles, ~385 miles) - counterparts to
# estimator/customer_simulator.md's personas A/B/C, which assume different addresses.
CUSTOMER_PERSONAS = {
    "cooperative": """You are a customer planning a move. Some information is already on
file from a document you provided earlier: moving from 123 Main St, Sacramento, CA
95814 to 456 Oak Ave, Los Angeles, CA 90012. Confirm those addresses if asked. You are
cooperative and give clear, complete answers when asked, but don't volunteer your whole
inventory unprompted - answer only what's asked. Origin is a 2-bedroom apartment, 2nd
floor, no elevator. Destination is a 1-bedroom apartment, 1st floor, with an elevator.
The distance is about 385 miles - give that as your best estimate if asked, you're not
100% sure. Move date is flexible, sometime in the next month - if pressed for a
concrete anchor date, say the first Saturday of next month. You have a sofa, queen bed,
dresser, refrigerator, and about 20 boxes - no piano, no storage needed. Only the queen
bed needs disassembly. You have a flat-screen TV as a fragile/high-value item. You want
full-service and do need packing materials provided. No stairs or long-carry issues
beyond the 2nd-floor walk-up at pickup. If the estimator's question sounds like a final
readback/summary, confirm it's correct and say you're done.""",
    "vague": """You are a customer planning a move who gives short, vague answers unless
pushed. Some information is already on file from a document you provided earlier:
moving from 123 Main St, Sacramento, CA 95814 to 456 Oak Ave, Los Angeles, CA 90012.
Same underlying move details as: 2-bedroom apartment (2nd floor, no elevator) to
1-bedroom apartment (1st floor, elevator), about 385 miles, sofa/queen bed/dresser/
fridge/20 boxes (only the bed needs disassembly), a flat-screen TV, full-service with
packing materials needed, no piano, no storage. But when asked what you're moving, say
"just normal apartment stuff" first - only give specifics if the agent asks follow-up
questions with concrete examples. If asked about stairs, say "I think there's a couple
steps? Not sure" until pressed for an exact number. Eventually give real answers if
pushed twice. If the estimator's question sounds like a final readback, confirm it.""",
    "contradicts": """You are a customer planning a move, same underlying details as: a
document already on file has moving from 123 Main St, Sacramento, CA 95814 to 456 Oak
Ave, Los Angeles, CA 90012, a 2-bedroom apartment (2nd floor, no elevator) to a
1-bedroom apartment (1st floor, elevator), about 385 miles, sofa/queen bed/dresser/
fridge/20 boxes (only the bed needs disassembly), a flat-screen TV, full-service with
packing materials needed, no storage. Early in the conversation say there's no piano.
Partway through, when discussing large items again or near the end, contradict
yourself: "oh wait, I forgot, I do have a piano too." A good estimator should catch
this at readback and include the piano in the final spec. Confirm the final readback
once it's corrected.""",
}

# quote_id -> (Caller persona key, company name, Closer scenario key). Closer scenario
# is None for companies that never yield a negotiable quote (the stonewaller only ever
# produces a callback_commitment).
CALLER_COMPANIES = {
    "carolina": {"caller_persona": "tough", "company_name": "Carolina Movers Co.", "closer_scenario": "tough"},
    "budget": {"caller_persona": "lowballer", "company_name": "Budget Move Solutions", "closer_scenario": "lowballer"},
    "premier": {"caller_persona": "upseller", "company_name": "Premier Relocation Services", "closer_scenario": "firm"},
    "reliable": {"caller_persona": "stonewaller", "company_name": "Reliable Movers LLC", "closer_scenario": None},
}

# Never called by The Caller - a fresh, suspiciously cheap quote gathered outside the
# normal call set, injected straight into the negotiation round as a red-flag stress
# test (see closer/negotiation_scenarios.md scenario 4 and
# closer/scripts/run_negotiation_round.py's QUICKHAUL_QUOTE).
QUICKHAUL_QUOTE_ID = "quickhaul"
QUICKHAUL_QUOTE = {"company_name": "QuickHaul Express", "total": 950, "binding": False}
QUICKHAUL_CLOSER_SCENARIO = "redflag"


def load_initial_spec() -> dict:
    return json.loads(INITIAL_SPEC_PATH.read_text())


def run_estimator_stage(client, initial_spec: dict, customer_persona: str, max_turns: int, show_transcript: bool):
    """Runs the voice-interview conversation seeded with initial_spec as document-intake
    context, extracts the final job spec, and returns (spec, transcript, run_dir)."""
    estimator_system = estimator_eval.load_estimator_prompt()
    customer_system = CUSTOMER_PERSONAS[customer_persona]

    opening = (
        "Begin the interview. A document intake step already ran and produced this "
        "partial job spec from information the customer provided earlier - confirm "
        "each field with the customer rather than asking from scratch, and fill in "
        "everything still null:\n\n```json\n" + json.dumps(initial_spec, indent=2) + "\n```"
    )

    estimator_history = [{"role": "user", "content": opening}]
    customer_history = []
    transcript_lines = []
    estimator_text = ""

    for _ in range(max_turns):
        estimator_text = estimator_eval.chat(client, estimator_system, estimator_history)
        estimator_history.append({"role": "assistant", "content": estimator_text})
        transcript_lines.append(f"ESTIMATOR: {estimator_text}")
        if show_transcript:
            print(f"\n[ESTIMATOR]: {estimator_text}")

        if "```json" in estimator_text or __import__("re").search(r'\{\s*"origin"', estimator_text):
            break

        customer_history.append({"role": "user", "content": estimator_text})
        customer_text = estimator_eval.chat(client, customer_system, customer_history)
        customer_history.append({"role": "assistant", "content": customer_text})
        transcript_lines.append(f"CUSTOMER: {customer_text}")
        if show_transcript:
            print(f"[CUSTOMER]: {customer_text}")

        estimator_history.append({"role": "user", "content": customer_text})

    transcript = "\n".join(transcript_lines)
    extracted = estimator_eval.extract_json_block(estimator_text)
    run_dir = estimator_eval.save_run(
        f"PIPELINE_{customer_persona}", transcript, extracted,
        {"note": "pipeline run - not graded, see run_estimator_eval_openai.py for graded eval runs"},
    )
    return extracted, transcript, run_dir


def validate_confirmed_spec(spec: dict | None) -> list[str]:
    """Reuses validate_job_spec.py's checks against an in-memory spec dict instead of a
    file path. Returns a list of problems - empty means the spec is safe to call on."""
    if spec is None:
        return ["The Estimator never produced a valid final JSON spec."]

    problems = []
    schema = validator.load_schema()
    schema_errors = sorted(validator.Draft7Validator(schema).iter_errors(spec), key=lambda e: e.path)
    for err in schema_errors:
        field_path = ".".join(str(p) for p in err.path) or "(root)"
        problems.append(f"schema: {field_path}: {err.message}")

    for path in validator.CRITICAL_FIELDS:
        if validator.get_nested(spec, path) is None:
            problems.append(f"missing critical field: {'.'.join(path)}")

    if not spec.get("user_confirmed", False):
        problems.append("user_confirmed is false/missing - the customer never confirmed the readback")

    return problems


def run_caller_stage(client, confirmed_spec: dict, companies: list[str], max_turns: int, show_transcript: bool) -> dict:
    """Calls each requested company and returns {quote_id: {..., outcome, transcript_ref}}."""
    results = {}
    for quote_id in companies:
        company = CALLER_COMPANIES[quote_id]
        print(f"\n=== Calling {company['company_name']} ({company['caller_persona']}) ===")
        transcript, final_text = caller_eval.run_conversation(
            client, company["caller_persona"], confirmed_spec, max_turns, show_transcript
        )
        extracted = caller_eval.extract_json_block(final_text)
        run_dir = caller_eval.save_run(
            company["caller_persona"], transcript, extracted,
            {"note": "pipeline run - not graded, see run_caller_eval_openai.py for graded eval runs"},
        )
        outcome_type = (extracted or {}).get("outcome_type")
        print(f"  Outcome: {outcome_type}")
        results[quote_id] = {
            "company_name": company["company_name"],
            "closer_scenario": company["closer_scenario"],
            "outcome": extracted or {},
            "transcript_ref": str((run_dir / "transcript.txt").relative_to(ROOT)),
        }
    return results


def compute_round_quotes(itemized: dict, threshold_pct: float) -> dict:
    """Pre-negotiation red-flag pass over itemized quotes only, for round eligibility
    per closer_orchestration.md - mirrors run_negotiation_round.py's build_round_quotes,
    parameterized by the threshold in closer/config/market_benchmarks.json."""
    totals = [q["total"] for q in itemized.values()]
    median = statistics.median(totals)
    cutoff = median * (1 - threshold_pct / 100)

    quotes = {}
    for qid, q in itemized.items():
        flagged = q["total"] <= cutoff
        pct_below = 100 * (1 - q["total"] / median)
        quotes[qid] = {
            "company_name": q["company_name"],
            "total": q["total"],
            "binding": q["binding"],
            "red_flag": {
                "flagged": flagged,
                "reason": (
                    f"${q['total']:,.0f} is {pct_below:.0f}% below the session median "
                    f"of ${median:,.0f}" if flagged else None
                ),
            },
        }
    return quotes


def display_quotes(state: dict) -> dict:
    """Adapts orchestration-shape quote state into the JSON block load_closer_prompt
    embeds for the closer to read - live, ratcheted numbers each call."""
    return {
        qid: {
            "company_name": q["company_name"],
            "current_total": q["total"],
            "binding": q["binding"],
            "red_flag": q["red_flag"],
        }
        for qid, q in state["quotes"].items()
    }


def run_closer_round(client, confirmed_spec: dict, itemized: dict, scenario_for_quote: dict,
                      threshold_pct: float, max_turns: int, show_transcript: bool):
    """Runs one full negotiation round over every itemized quote, ratcheting
    current_best_offer call by call per closer_orchestration.md. Returns
    (final_state, {quote_id: closer_outcome_dict})."""
    if not itemized:
        return {"quotes": {}, "current_best_offer": None, "queue": []}, {}

    round_quotes = compute_round_quotes(itemized, threshold_pct)
    state = planner.start_round(round_quotes)

    print("\n=== CLOSER: negotiation round ===")
    print("Initial current_best_offer:", json.dumps(state["current_best_offer"], indent=2))
    for qid, q in state["quotes"].items():
        if q["red_flag"]["flagged"]:
            print(f"  [red-flagged at round start] {q['company_name']}: {q['red_flag']['reason']}")

    calls = []
    closer_outcomes = {}
    while state["queue"]:
        qid = state["queue"][0]
        scenario = closer_eval.SCENARIOS[scenario_for_quote[qid]]
        leverage = planner.next_call_leverage(state)

        print(f"\n--- Calling back {state['quotes'][qid]['company_name']} ---")
        if leverage:
            print(f"  Leverage to cite: ${leverage['total']:,.0f} from {leverage['company_name']} (binding={leverage['binding']})")
        else:
            print("  No leverage to cite - fees/terms pushback only.")

        closer_system = closer_eval.load_closer_prompt(
            confirmed_spec, qid, current_best_offer=leverage, quotes=display_quotes(state)
        )
        transcript, final_text = closer_eval.run_conversation(
            client, closer_system, scenario["system"], scenario["name"], max_turns, show_transcript
        )
        extracted = closer_eval.extract_json_block(final_text)
        closer_outcomes[qid] = extracted or {}

        current = state["quotes"][qid]
        negotiated_total = (extracted or {}).get("negotiated_total")
        call_result = {
            "quote_id": qid,
            "total": negotiated_total if negotiated_total is not None else current["total"],
            "binding": (extracted or {}).get("binding", current["binding"]),
            "red_flag": (extracted or {}).get("red_flag") or current["red_flag"],
            "source": "negotiated" if negotiated_total is not None else "callback_pending",
        }
        state = planner.advance_round(state, call_result)
        calls.append({
            "quote_id": qid, "company_name": current["company_name"], "transcript": transcript,
            "outcome": extracted, "call_result": call_result,
        })
        print(f"  Result: total=${call_result['total']:,.0f} binding={call_result['binding']} red_flag={call_result['red_flag']}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    round_dir = ROOT / "closer" / "transcripts" / f"round_{ts}"
    round_dir.mkdir(parents=True, exist_ok=True)
    for call in calls:
        call_dir = round_dir / call["quote_id"]
        call_dir.mkdir(parents=True, exist_ok=True)
        (call_dir / "transcript.txt").write_text(call["transcript"])
        (call_dir / "outcome.json").write_text(json.dumps(call["outcome"], indent=2) if call["outcome"] else "null")
    summary = {
        "call_order": [c["quote_id"] for c in calls],
        "final_current_best_offer": state["current_best_offer"],
        "final_quotes": state["quotes"],
    }
    (round_dir / "round_summary.json").write_text(json.dumps(summary, indent=2))
    closer_eval.prune_old_transcripts(round_dir.parent)
    print(f"\nSaved negotiation round to: {round_dir}")

    for qid in closer_outcomes:
        closer_outcomes[qid]["_transcript_ref"] = str((round_dir / qid / "transcript.txt").relative_to(ROOT))

    return state, closer_outcomes


def build_quotes_input(caller_results: dict, closer_outcomes: dict, include_redflag: bool) -> list:
    quotes_input = []

    for qid, result in caller_results.items():
        outcome = result["outcome"]
        outcome_type = outcome.get("outcome_type")
        entry = {
            "quote_id": qid,
            "company_name": result["company_name"],
            "call_outcome_type": outcome_type,
            "transcript_refs": [result["transcript_ref"]],
        }
        if outcome_type == "itemized_quote":
            entry["original_total"] = outcome.get("total_estimate")
            entry["binding"] = outcome.get("binding")
            entry["fees"] = outcome.get("fees", [])
            entry["deposit_required"] = outcome.get("deposit_required")
            entry["deposit_refundable"] = outcome.get("deposit_refundable")
            entry["cancellation_policy"] = outcome.get("cancellation_policy")

        closer_outcome = closer_outcomes.get(qid)
        if closer_outcome:
            entry["negotiated_total"] = closer_outcome.get("negotiated_total")
            entry["negotiation_outcome_type"] = closer_outcome.get("outcome_type")
            if closer_outcome.get("binding") is not None:
                entry["binding"] = closer_outcome["binding"]
            if closer_outcome.get("fees"):
                entry["fees"] = closer_outcome["fees"]
            entry["concessions"] = closer_outcome.get("concessions", [])
            if closer_outcome.get("red_flag"):
                entry["red_flag"] = closer_outcome["red_flag"]
            if closer_outcome.get("_transcript_ref"):
                entry["transcript_refs"].append(closer_outcome["_transcript_ref"])

        quotes_input.append(entry)

    if include_redflag and QUICKHAUL_QUOTE_ID in closer_outcomes:
        closer_outcome = closer_outcomes[QUICKHAUL_QUOTE_ID]
        entry = {
            "quote_id": QUICKHAUL_QUOTE_ID,
            "company_name": QUICKHAUL_QUOTE["company_name"],
            "call_outcome_type": "itemized_quote",
            "source_note": "external quote, not gathered via The Caller - injected as a red-flag stress test",
            "original_total": QUICKHAUL_QUOTE["total"],
            "binding": QUICKHAUL_QUOTE["binding"],
            "fees": [],
            "negotiated_total": closer_outcome.get("negotiated_total"),
            "negotiation_outcome_type": closer_outcome.get("outcome_type"),
            "concessions": closer_outcome.get("concessions", []),
            "transcript_refs": [closer_outcome["_transcript_ref"]] if closer_outcome.get("_transcript_ref") else [],
        }
        if closer_outcome.get("binding") is not None:
            entry["binding"] = closer_outcome["binding"]
        if closer_outcome.get("fees"):
            entry["fees"] = closer_outcome["fees"]
        if closer_outcome.get("red_flag"):
            entry["red_flag"] = closer_outcome["red_flag"]
        quotes_input.append(entry)

    return quotes_input


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--customer-persona", choices=list(CUSTOMER_PERSONAS.keys()), default="cooperative")
    parser.add_argument("--companies", default="carolina,budget,premier,reliable",
                         help="Comma-separated subset of: " + ",".join(CALLER_COMPANIES.keys()))
    parser.add_argument("--include-redflag", dest="include_redflag", action="store_true", default=True)
    parser.add_argument("--no-redflag", dest="include_redflag", action="store_false")
    parser.add_argument("--turns-estimator", type=int, default=30)
    parser.add_argument("--turns-caller", type=int, default=14)
    parser.add_argument("--turns-closer", type=int, default=14)
    parser.add_argument("--show-transcript", action="store_true")
    args = parser.parse_args()

    companies = [c.strip() for c in args.companies.split(",") if c.strip()]
    for c in companies:
        if c not in CALLER_COMPANIES:
            sys.exit(f"Unknown company '{c}'. Choices: {', '.join(CALLER_COMPANIES.keys())}")

    client = OpenAI()  # reads OPENAI_API_KEY from env

    # --- Stage 1: Estimator ---
    print("=== ESTIMATOR: intake interview ===")
    initial_spec = load_initial_spec()
    confirmed_spec, _, estimator_run_dir = run_estimator_stage(
        client, initial_spec, args.customer_persona, args.turns_estimator, args.show_transcript
    )

    problems = validate_confirmed_spec(confirmed_spec)
    if problems:
        print("\nSTOPPING - confirmed job spec failed validation, no calls will be placed:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    confirmed_spec["spec_version_hash"] = validator.compute_spec_hash(confirmed_spec)
    print(f"\nConfirmed job spec is valid. hash={confirmed_spec['spec_version_hash']}")
    print(f"Estimator transcript saved to: {estimator_run_dir}")

    # --- Stage 2: Caller ---
    print("\n=== CALLER: gathering quotes ===")
    caller_results = run_caller_stage(client, confirmed_spec, companies, args.turns_caller, args.show_transcript)

    itemized = {
        qid: {"company_name": r["company_name"], "total": r["outcome"]["total_estimate"], "binding": r["outcome"]["binding"]}
        for qid, r in caller_results.items()
        if r["outcome"].get("outcome_type") == "itemized_quote" and CALLER_COMPANIES[qid]["closer_scenario"]
    }
    scenario_for_quote = {qid: CALLER_COMPANIES[qid]["closer_scenario"] for qid in itemized}

    if args.include_redflag:
        itemized[QUICKHAUL_QUOTE_ID] = dict(QUICKHAUL_QUOTE)
        scenario_for_quote[QUICKHAUL_QUOTE_ID] = QUICKHAUL_CLOSER_SCENARIO

    # --- Stage 3: Closer ---
    benchmarks = json.loads((ROOT / "closer" / "config" / "market_benchmarks.json").read_text())
    threshold_pct = benchmarks["red_flag_rule"]["session_comparison"]["threshold_pct_below_median"]
    _, closer_outcomes = run_closer_round(
        client, confirmed_spec, itemized, scenario_for_quote, threshold_pct,
        args.turns_closer, args.show_transcript,
    )

    # --- Stage 4: Report ---
    print("\n=== REPORT: building ranked comparison ===")
    quotes_input = build_quotes_input(caller_results, closer_outcomes, args.include_redflag)
    report = reporter.build_report(quotes_input, confirmed_spec)
    errors = reporter.validate_report(report)
    if errors:
        print(f"SCHEMA WARNINGS ({len(errors)}):")
        for err in errors:
            field_path = ".".join(str(p) for p in err.path) or "(root)"
            print(f"  - {field_path}: {err.message}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = PIPELINE_RUNS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "confirmed_job_spec.json").write_text(json.dumps(confirmed_spec, indent=2))
    (run_dir / "quotes_input.json").write_text(json.dumps(quotes_input, indent=2))
    (run_dir / "comparison_report.json").write_text(json.dumps(report, indent=2))

    print(f"\n{report['recommendation_explanation']}")
    print(f"\nPipeline run complete. Full artifacts saved to: {run_dir}")


if __name__ == "__main__":
    main()
