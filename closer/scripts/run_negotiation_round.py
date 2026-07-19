"""
Drives one full negotiation round across all four callback personas as a live
sequential reverse auction, per closer_orchestration.md: call every company in
queue order (highest original quote to lowest, current-best-holder called last),
ratchet current_best_offer after every real call result via
plan_negotiation_round.advance_round, and exclude red-flagged quotes from being
cited as leverage until a callback clears them.

run_closer_eval_openai.py only ever tests one company in isolation, recomputing
leverage from the static GATHERED_QUOTES snapshot each time - nothing carries state
between calls. This script is the missing piece: it wires plan_negotiation_round.py's
state machine to the same simulated chat() calls, so current_best_offer actually
ratchets and red-flag exclusion actually takes effect across a full round.

QuickHaul Express (the redflag persona) isn't in GATHERED_QUOTES - that file treats
it as a fresh quote outside the original call set for single-call testing. Here it's
a full participant in the round with its own gathered-quote entry, so its red flag
gets computed up front (like a real quote would be) and it can be displaced from - or
displace - current_best_offer like any other company.

Setup: same as run_closer_eval_openai.py (pip install openai, export OPENAI_API_KEY).

Usage:
    python run_negotiation_round.py
    python run_negotiation_round.py --turns 12 --show-transcript
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from plan_negotiation_round import advance_round, next_call_leverage, start_round
from run_closer_eval_openai import (
    DEFAULT_JOB_SPEC,
    GATHERED_QUOTES,
    SCENARIOS,
    TRANSCRIPTS_DIR,
    extract_json_block,
    load_closer_prompt,
    run_conversation,
)

# quote_id -> the run_closer_eval_openai.py scenario whose persona plays that company.
SCENARIO_FOR_QUOTE = {"carolina": "tough", "budget": "lowballer", "premier": "firm", "quickhaul": "redflag"}

# Not in GATHERED_QUOTES (see module docstring) - a real round participant needs one.
QUICKHAUL_QUOTE = {"company_name": "QuickHaul Express", "original_total": 950, "binding": False}

RED_FLAG_THRESHOLD_PCT = 30  # see closer/config/market_benchmarks.json: session_comparison rule


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2


def build_round_quotes() -> dict:
    """All four companies' original quotes, each pre-checked for the red-flag session-
    comparison rule (>=3 quotes gathered here, so the static benchmark fallback in
    market_benchmarks.json never applies) - matches 'before the call' in
    closer_agent.md's Red-flag check, done once for the whole round up front."""
    raw = dict(GATHERED_QUOTES)
    raw["quickhaul"] = QUICKHAUL_QUOTE

    median = _median([q["original_total"] for q in raw.values()])
    threshold = median * (1 - RED_FLAG_THRESHOLD_PCT / 100)

    quotes = {}
    for qid, q in raw.items():
        flagged = q["original_total"] <= threshold
        pct_below = 100 * (1 - q["original_total"] / median)
        quotes[qid] = {
            "company_name": q["company_name"],
            "total": q["original_total"],
            "binding": q["binding"],
            "red_flag": {
                "flagged": flagged,
                "reason": f"${q['original_total']:,.0f} is {pct_below:.0f}% below the session median of ${median:,.0f}" if flagged else None,
            },
        }
    return quotes


def _display_quotes(state: dict) -> dict:
    """Adapts the orchestration-shape quote state (see plan_negotiation_round.py) into
    the JSON block load_closer_prompt embeds for the closer to read - live, ratcheted
    numbers, not the static GATHERED_QUOTES snapshot."""
    return {
        qid: {
            "company_name": q["company_name"],
            "current_total": q["total"],
            "binding": q["binding"],
            "red_flag": q["red_flag"],
        }
        for qid, q in state["quotes"].items()
    }


def run_round(client, job_spec: dict, max_turns: int, show_transcript: bool):
    state = start_round(build_round_quotes())

    print("Initial current_best_offer:", json.dumps(state["current_best_offer"], indent=2))
    print("Call queue:", state["queue"])
    for qid, q in state["quotes"].items():
        if q["red_flag"]["flagged"]:
            print(f"  [red-flagged at round start] {q['company_name']}: {q['red_flag']['reason']}")

    calls = []
    while state["queue"]:
        qid = state["queue"][0]
        scenario = SCENARIOS[SCENARIO_FOR_QUOTE[qid]]
        leverage = next_call_leverage(state)

        print(f"\n=== Calling {scenario['name']} ===")
        if leverage:
            print(f"  Leverage to cite: ${leverage['total']:,.0f} from {leverage['company_name']} (binding={leverage['binding']})")
        else:
            print("  No leverage to cite - fees/terms pushback only.")

        closer_system = load_closer_prompt(
            job_spec, qid, current_best_offer=leverage, quotes=_display_quotes(state)
        )
        transcript, final_closer_text = run_conversation(
            client, closer_system, scenario["system"], scenario["name"], max_turns, show_transcript
        )
        extracted = extract_json_block(final_closer_text)

        current = state["quotes"][qid]
        negotiated_total = (extracted or {}).get("negotiated_total")
        call_result = {
            "quote_id": qid,
            "total": negotiated_total if negotiated_total is not None else current["total"],
            "binding": (extracted or {}).get("binding", current["binding"]),
            "red_flag": (extracted or {}).get("red_flag") or current["red_flag"],
            "source": "negotiated" if negotiated_total is not None else "callback_pending",
        }
        state = advance_round(state, call_result)

        calls.append({"quote_id": qid, "company_name": current["company_name"], "transcript": transcript,
                       "outcome": extracted, "call_result": call_result})
        print(f"  Result: total=${call_result['total']:,.0f} binding={call_result['binding']} "
              f"red_flag={call_result['red_flag']}")
        print(f"  current_best_offer now: {json.dumps(state['current_best_offer'], indent=2)}")

    return state, calls


def save_round(final_state: dict, calls: list) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    round_dir = TRANSCRIPTS_DIR / f"round_{ts}"
    round_dir.mkdir(parents=True, exist_ok=True)

    for call in calls:
        call_dir = round_dir / call["quote_id"]
        call_dir.mkdir(parents=True, exist_ok=True)
        (call_dir / "transcript.txt").write_text(call["transcript"])
        (call_dir / "outcome.json").write_text(json.dumps(call["outcome"], indent=2) if call["outcome"] else "null")

    summary = {
        "call_order": [c["quote_id"] for c in calls],
        "final_current_best_offer": final_state["current_best_offer"],
        "final_quotes": final_state["quotes"],
    }
    (round_dir / "round_summary.json").write_text(json.dumps(summary, indent=2))
    return round_dir


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--turns", type=int, default=14)
    parser.add_argument("--show-transcript", action="store_true")
    parser.add_argument("--job-spec", type=str, default=str(DEFAULT_JOB_SPEC))
    args = parser.parse_args()

    job_spec = json.loads(Path(args.job_spec).read_text())
    client = OpenAI()

    final_state, calls = run_round(client, job_spec, args.turns, args.show_transcript)

    print("\n=== ROUND COMPLETE ===")
    print("Final current_best_offer:", json.dumps(final_state["current_best_offer"], indent=2))
    for q in final_state["quotes"].values():
        flag = " [RED FLAGGED]" if q["red_flag"]["flagged"] else ""
        binding = "binding" if q["binding"] else "non-binding"
        print(f"  {q['company_name']}: ${q['total']:,.0f} ({binding}){flag}")

    round_dir = save_round(final_state, calls)
    print(f"\nSaved round to: {round_dir}")


if __name__ == "__main__":
    main()
