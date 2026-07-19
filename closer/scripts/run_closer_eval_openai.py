"""
Runs the Closer agent against a simulated negotiation-callback counterparty persona,
extracts the final structured negotiation outcome JSON, then grades the result.

Setup:
    pip install openai --break-system-packages
    export OPENAI_API_KEY=your_key_here

Usage:
    python run_closer_eval_openai.py --scenario tough
    python run_closer_eval_openai.py --scenario upseller --show-transcript
    python run_closer_eval_openai.py --scenario lowballer --turns 16
    python run_closer_eval_openai.py --scenario redflag

Requires a confirmed job spec at schemas/examples/valid_spec_example.json (or point
--job-spec at your own file), and the gathered quote set below (matching the shared
setup in closer/negotiation_scenarios.md) - the closer needs real quotes to leverage.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency. Install with:\n    pip install openai --break-system-packages")

from plan_negotiation_round import compute_current_best

MODEL = "gpt-4o-mini"
CLOSER_DIR = Path(__file__).parent.parent
TRANSCRIPTS_DIR = CLOSER_DIR / "transcripts"
DEFAULT_JOB_SPEC = CLOSER_DIR.parent / "schemas" / "examples" / "valid_spec_example.json"

# The gathered quote set every scenario negotiates against - matches the shared setup
# in closer/negotiation_scenarios.md. The scenario under test is called back; whether
# the OTHER entries are usable as leverage on that call depends on current_best_offer
# (see compute_leverage below) - a company already holding the cheapest quote in the
# set gets no price leverage cited against it, per closer_orchestration.md.
GATHERED_QUOTES = {
    "carolina": {
        "company_name": "Carolina Movers Co.",
        "original_total": 2400,
        "binding": True,
        "fees": [{"name": "fuel surcharge", "amount": 150}, {"name": "stairs fee", "amount": 75}],
    },
    "budget": {
        "company_name": "Budget Move Solutions",
        "original_total": 1500,
        "binding": False,
        "fees": [
            {"name": "fuel surcharge", "amount": 100},
            {"name": "long carry fee", "amount": 150},
            {"name": "overtime hourly rate", "amount": 50},
        ],
    },
    "premier": {
        "company_name": "Premier Relocation Services",
        "original_total": 3650,
        "binding": False,
        "fees": [
            {"name": "insurance coverage", "amount": 300},
            {"name": "white glove packing", "amount": 400},
            {"name": "guaranteed delivery window", "amount": 150},
        ],
    },
}

SCENARIOS = {
    "tough": {
        "name": "Will match if pushed (Carolina Movers Co.)",
        "target_quote_key": "carolina",
        "system": """You are the same dispatcher at "Carolina Movers Co." from the
earlier call. You already quoted $2,400 (binding) plus $150 fuel and $75 stairs. When
the caller cites a competing quote around $1,975-$2,000, push back once ("that's a
tight margin") then come down to $2,200 if they hold firm on citing the number. You
won't go below $2,200. You're willing to make the deposit refundable (from
non-refundable) as a secondary concession if they push on that specifically, even
after settling the price.""",
    },
    "firm": {
        "name": "Firm, holds price (Premier Relocation Services)",
        "target_quote_key": "premier",
        "system": """You are the same salesperson at "Premier Relocation Services" from
the earlier call. Your quote was $2,800 plus add-ons ($3,650 total). When pushed with a
competing quote, you do not move the base price - you say "our price reflects the
service level, I can't match a bare-bones quote." You ARE willing to drop one add-on
(the $150 guaranteed delivery window fee) as a goodwill gesture if the caller pushes on
fees specifically, but the $2,800 base and the other add-ons stay. Do not invent any
new discount beyond this.""",
    },
    "lowballer": {
        "name": "Concedes on fees, not base price (Budget Move Solutions)",
        "target_quote_key": "budget",
        "system": """You are the same dispatcher at "Budget Move Solutions" from the
earlier call. Your original headline was $1,200, but the real total with fees is
closer to $1,500. When the caller calls back citing a competing number, you don't touch
the $1,200 headline - but if pressed specifically on the fuel surcharge or the
long-carry fee, you'll waive the $50/hour overtime fee as a concession. If the caller
points out your original quote is unusually low and asks whether anything's missing,
admit the fuel surcharge and long-carry fee again (same as the first call) rather than
denying them.""",
    },
    "redflag": {
        "name": "Suspicious lowball, tests red-flag discipline (QuickHaul Express)",
        "target_quote_key": None,
        "system": """You are a dispatcher at "QuickHaul Express," a company not in the
original call set - imagine this is a fresh quote the customer got separately at $950
for the same move, well below every other quote. When called back and asked to confirm
what's included, be vague at first ("it's a flat rate, don't worry about it") and only
reveal under direct questioning that the $950 doesn't include fuel, doesn't include the
stairs fee, and requires cash payment with no receipt.""",
    },
}

EVALUATOR_SYSTEM = """You are grading a negotiation-callback agent's performance. This
agent already has an original quote from this company (gathered by a separate calling
agent) and is calling back to negotiate using current_best_offer - the single cheapest
eligible quote across the whole gathered set - as leverage, where one exists. You will
be given ORIGINAL QUOTE, OTHER QUOTES, CURRENT BEST OFFER, PERSONA TYPE, TRANSCRIPT,
and EXTRACTED OUTCOME.

Score 0-2 each:

- leverage_accuracy: if CURRENT BEST OFFER is non-null, the agent must cite that exact
  number (amount and binding/non-binding status) as its price leverage - no rounding,
  no invented company, and no substituting a different real quote instead (even one
  that's factually accurate is a misuse if it isn't current_best_offer, since it isn't
  actually a competing improvement over this company's own number). If CURRENT BEST
  OFFER is null, the agent must not fabricate "can you beat this" price-leverage
  framing around any other quote - it should negotiate fees/terms only. Any mismatch
  or misuse is an automatic 0.
- fee_pushback: did the agent push on at least one specific fee or term (deposit,
  surcharge, cancellation policy), not just the headline total? A call that only
  repeats "can you do better on the total" without ever naming a specific fee scores
  at most 1.
- red_flag_handling: if ORIGINAL QUOTE is 30%+ below the median of OTHER QUOTES (or
  flagged in the extracted outcome), did the agent actually probe for missing fees
  instead of treating the low number as a clean win? If no red flag applies here,
  score this 2 by default.
- price_movement_logging: does the extracted JSON accurately reflect what happened on
  the call - if the price or a fee changed, is negotiated_total and concessions
  updated to match the transcript? If nothing moved, is negotiated_total correctly left
  equal to original_total rather than falsely showing a concession?
- structured_outcome: the call ends in exactly one of price_matched /
  partial_concession / held_firm / callback_commitment / documented_decline, as valid
  JSON, and the label matches what the transcript actually supports.

For every axis scored below 2, "failures" MUST include a short bullet citing the
specific gap.

Output ONLY this JSON, no other text:
{"scores": {"leverage_accuracy": 0, "fee_pushback": 0, "red_flag_handling": 0,
"price_movement_logging": 0, "structured_outcome": 0}, "total": 0,
"failures": ["short bullet per any score below 2, citing the specific gap"],
"pass": false}
pass is true only if total >= 8 AND leverage_accuracy == 2."""


def compute_leverage(target_key: str | None) -> dict | None:
    """The current_best_offer to hand this call, per closer_orchestration.md: the
    cheapest eligible quote across the WHOLE gathered set (including the target
    itself). Returns None if the target company already holds that number - there's
    nothing cheaper to cite, so this call gets fee/terms-only framing instead of
    price leverage."""
    quotes_for_orchestration = {
        qid: {"company_name": q["company_name"], "total": q["original_total"], "binding": q["binding"]}
        for qid, q in GATHERED_QUOTES.items()
    }
    current_best = compute_current_best(quotes_for_orchestration)
    if current_best is None or current_best["quote_id"] == target_key:
        return None
    return current_best


_LEVERAGE_AUTO = object()  # sentinel: "compute it from GATHERED_QUOTES" vs. an explicit override


def load_closer_prompt(
    job_spec: dict,
    target_key: str | None,
    current_best_offer=_LEVERAGE_AUTO,
    quotes: dict | None = None,
) -> str:
    """quotes/current_best_offer let a live round driver (see
    run_negotiation_round.py) inject the ratcheted, per-call-updated quote set and
    leverage instead of the static GATHERED_QUOTES snapshot used for one-off testing
    here. Defaults preserve the original single-call behavior."""
    base = (CLOSER_DIR / "closer_agent.md").read_text()
    benchmarks = json.loads((CLOSER_DIR / "config" / "market_benchmarks.json").read_text())

    quotes = quotes if quotes is not None else GATHERED_QUOTES
    other_quotes = {k: v for k, v in quotes.items() if k != target_key}
    original_quote = quotes.get(target_key)
    if current_best_offer is _LEVERAGE_AUTO:
        current_best_offer = compute_leverage(target_key)

    context = f"\n\n## The job you are calling about\n```json\n{json.dumps(job_spec, indent=2)}\n```"
    context += f"\n\n## Market benchmarks / red-flag config\n```json\n{json.dumps(benchmarks, indent=2)}\n```"
    if original_quote:
        context += f"\n\n## This company's original quote (from The Caller)\n```json\n{json.dumps(original_quote, indent=2)}\n```"
    else:
        context += "\n\n## This company's original quote\nThis is a fresh quote gathered outside the original call set - $950, no fee breakdown given yet."
    context += f"\n\n## Other real quotes gathered so far (full set, for grounding only - never invent numbers outside this set)\n```json\n{json.dumps(other_quotes, indent=2)}\n```"
    if current_best_offer:
        context += f"\n\n## current_best_offer (the ONE number to cite as price leverage on this call)\n```json\n{json.dumps(current_best_offer, indent=2)}\n```"
    else:
        context += (
            "\n\n## current_best_offer\nnull - this company's original quote already holds "
            "the best number gathered so far. Per closer_agent.md's 'no leverage available' "
            "branch: skip the \"can you beat this\" framing entirely and negotiate purely on "
            "fees, deposit terms, and extras instead."
        )

    return base + context


def chat(client, system: str, messages: list) -> str:
    full_messages = [{"role": "system", "content": system}] + messages
    resp = client.chat.completions.create(model=MODEL, messages=full_messages)
    return resp.choices[0].message.content


def run_conversation(
    client,
    closer_system: str,
    counterparty_system: str,
    counterparty_label: str,
    max_turns: int,
    show_transcript: bool,
):
    closer_history = [{"role": "user", "content": "Begin the callback: reference the earlier quote and open the negotiation."}]
    counterparty_history = []
    transcript_lines = []
    closer_text = ""

    for turn in range(max_turns):
        closer_text = chat(client, closer_system, closer_history)
        closer_history.append({"role": "assistant", "content": closer_text})
        transcript_lines.append(f"CLOSER: {closer_text}")
        if show_transcript:
            print(f"\n[CLOSER]: {closer_text}")

        if re.search(r'\{\s*"outcome_type"', closer_text):
            break

        counterparty_history.append({"role": "user", "content": closer_text})
        counterparty_text = chat(client, counterparty_system, counterparty_history)
        counterparty_history.append({"role": "assistant", "content": counterparty_text})
        transcript_lines.append(f"COUNTERPARTY ({counterparty_label}): {counterparty_text}")
        if show_transcript:
            print(f"[COUNTERPARTY]: {counterparty_text}")

        closer_history.append({"role": "user", "content": counterparty_text})

    transcript = "\n".join(transcript_lines)
    return transcript, closer_text


def extract_json_block(text: str) -> dict | None:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def grade(client, scenario_key: str, transcript: str, extracted: dict) -> dict:
    scenario = SCENARIOS[scenario_key]
    target_key = scenario["target_quote_key"]
    original_quote = GATHERED_QUOTES.get(target_key, {"note": "fresh quote not in original call set - $950, no breakdown"})
    other_quotes = {k: v for k, v in GATHERED_QUOTES.items() if k != target_key}
    current_best_offer = compute_leverage(target_key)

    user_content = (
        f"ORIGINAL QUOTE:\n{json.dumps(original_quote, indent=2)}\n\n"
        f"OTHER QUOTES:\n{json.dumps(other_quotes, indent=2)}\n\n"
        f"CURRENT BEST OFFER (the only legitimate price-leverage number for this call; "
        f"null means this company already holds the best price and no price leverage "
        f"should be cited):\n{json.dumps(current_best_offer, indent=2) if current_best_offer else 'null'}\n\n"
        f"PERSONA TYPE: {scenario['name']}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        f"EXTRACTED OUTCOME:\n{json.dumps(extracted, indent=2) if extracted else 'NONE - agent never produced a structured outcome'}"
    )
    result_text = chat(client, EVALUATOR_SYSTEM, [{"role": "user", "content": user_content}])
    result = extract_json_block(result_text)
    return result or {"error": "evaluator did not return valid JSON", "raw": result_text}


def check_unresolved_offer(transcript: str, extracted: dict | None, original_total: float | None) -> list:
    """Deterministic safety net for the 'never let a concrete number go unlogged' rule
    in closer_agent.md. Catches the case where the counterparty stated a real dollar
    figure below the original total somewhere on the call, but the extracted outcome
    still claims no movement happened at all (held_firm / documented_decline with
    negotiated_total left null or unchanged). This is a heuristic - it flags for human
    review rather than silently rewriting the outcome, since a $ mention isn't always
    the final offer (it could be a fee being discussed, not the total)."""
    if not extracted or original_total is None:
        return []

    counterparty_lines = [line for line in transcript.splitlines() if line.startswith("COUNTERPARTY")]
    amounts = []
    for line in counterparty_lines:
        amounts.extend(float(m.replace(",", "")) for m in re.findall(r"\$([\d,]+(?:\.\d+)?)", line))
    lower_offers = [a for a in amounts if a < original_total]
    if not lower_offers:
        return []

    outcome_type = extracted.get("outcome_type")
    negotiated_total = extracted.get("negotiated_total")
    no_movement_logged = negotiated_total is None or negotiated_total == extracted.get("original_total")

    if outcome_type in ("held_firm", "documented_decline") and no_movement_logged:
        return [
            f"Counterparty stated ${lower_offers[-1]:,.0f} on the call (below the "
            f"${original_total:,.0f} original), but the outcome logs '{outcome_type}' "
            f"with negotiated_total={negotiated_total!r} - a concrete offer may have "
            f"been left unresolved. See closer_agent.md: 'Never let a concrete number "
            f"go unlogged.'"
        ]
    return []


def save_run(scenario_key: str, transcript: str, extracted: dict, grading: dict) -> Path:
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = TRANSCRIPTS_DIR / f"{scenario_key}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "transcript.txt").write_text(transcript)
    (run_dir / "outcome.json").write_text(json.dumps(extracted, indent=2) if extracted else "null")
    (run_dir / "grading.json").write_text(json.dumps(grading, indent=2))

    return run_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), required=True)
    parser.add_argument("--turns", type=int, default=14)
    parser.add_argument("--show-transcript", action="store_true")
    parser.add_argument("--job-spec", type=str, default=str(DEFAULT_JOB_SPEC))
    args = parser.parse_args()

    job_spec = json.loads(Path(args.job_spec).read_text())
    client = OpenAI()

    scenario = SCENARIOS[args.scenario]
    print(f"Calling back scenario: {scenario['name']}...")
    closer_system = load_closer_prompt(job_spec, scenario["target_quote_key"])
    transcript, final_closer_text = run_conversation(
        client, closer_system, scenario["system"], scenario["name"], args.turns, args.show_transcript
    )
    extracted = extract_json_block(final_closer_text)

    print("\n--- GRADING ---")
    result = grade(client, args.scenario, transcript, extracted)

    target_key = SCENARIOS[args.scenario]["target_quote_key"]
    original_quote = GATHERED_QUOTES.get(target_key)
    original_total = original_quote["original_total"] if original_quote else None
    consistency_warnings = check_unresolved_offer(transcript, extracted, original_total)
    if consistency_warnings:
        result["consistency_warnings"] = consistency_warnings
        print("\n--- CONSISTENCY CHECK (deterministic, not the LLM grader) ---")
        for warning in consistency_warnings:
            print(f"  - {warning}")

    print(json.dumps(result, indent=2))

    run_dir = save_run(args.scenario, transcript, extracted, result)
    print(f"\nSaved transcript, outcome, and grading to: {run_dir}")


if __name__ == "__main__":
    main()
