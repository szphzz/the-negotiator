"""
Reference implementation of the call-order and ratcheting current_best_offer logic
described in closer/closer_orchestration.md. Drives one negotiation round: which
company to call next, what current_best_offer to hand closer_agent.md for that call,
and how current_best_offer updates after each real call outcome comes back.

This is a plain driver, not a simulator - it doesn't make calls or invent outcomes.
Feed it real quotes (from The Caller) to start, then feed each real Closer call result
back in via advance_round() as it happens.

Usage as a library:
    from plan_negotiation_round import start_round, advance_round

    state = start_round(gathered_quotes)
    while state["queue"]:
        next_quote_id = state["queue"][0]
        leverage = state["current_best_offer"] if next_quote_id != state["current_best_offer"]["quote_id"] else None
        # ... hand (quotes[next_quote_id], leverage) to closer_agent.md, run the call ...
        # result = {"quote_id": next_quote_id, "total": ..., "binding": ..., "red_flag": {...}}
        state = advance_round(state, result)

Usage from the command line (prints the plan for a static quote set, no live calls):
    python plan_negotiation_round.py gathered_quotes.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _is_red_flagged(quote: dict) -> bool:
    return bool((quote.get("red_flag") or {}).get("flagged"))


def _is_better(candidate: dict, current: dict | None) -> bool:
    """candidate/current shape: {quote_id, company_name, total, binding, source}.
    Lower total wins among eligible (non-red-flagged) quotes; binding is preferred
    over non-binding at the same or better price; a non-binding quote never displaces
    a binding one unless it's cheaper AND no binding quote is available at all."""
    if current is None:
        return True
    if candidate["total"] < current["total"]:
        return True
    if candidate["total"] == current["total"] and candidate["binding"] and not current["binding"]:
        return True
    return False


def _eligible_quotes(quotes: dict) -> dict:
    return {qid: q for qid, q in quotes.items() if not _is_red_flagged(q)}


def compute_current_best(quotes: dict) -> dict | None:
    """Recomputes current_best_offer from scratch over the given quote set - used both
    to seed the round and as the ground-truth check in advance_round()."""
    best = None
    for qid, q in _eligible_quotes(quotes).items():
        candidate = {
            "quote_id": qid,
            "company_name": q["company_name"],
            "total": q["total"],
            "binding": q["binding"],
            "source": q.get("source", "original"),
        }
        if _is_better(candidate, best):
            best = candidate
    return best


def start_round(gathered_quotes: dict) -> dict:
    """gathered_quotes: {quote_id: {company_name, total, binding, red_flag?}} - the
    Caller's original quotes for this job. Returns the initial state: current_best_offer
    plus the call queue, ordered highest original total to lowest, with the company
    that IS current_best_offer moved to the end (it gets a different call framing)."""
    quotes = {qid: dict(q, source=q.get("source", "original")) for qid, q in gathered_quotes.items()}
    current_best = compute_current_best(quotes)

    others = [qid for qid in quotes if current_best is None or qid != current_best["quote_id"]]
    others.sort(key=lambda qid: quotes[qid]["total"], reverse=True)

    queue = others + ([current_best["quote_id"]] if current_best else [])

    return {"quotes": quotes, "current_best_offer": current_best, "queue": queue}


def advance_round(state: dict, call_result: dict) -> dict:
    """call_result: {quote_id, total, binding, red_flag?} - the real outcome of the
    call that was just placed against state["queue"][0]. Updates the stored quote for
    that company, recomputes current_best_offer, advances the queue, and returns the
    new state for the next call."""
    quotes = dict(state["quotes"])
    qid = call_result["quote_id"]
    if state["queue"][0] != qid:
        raise ValueError(f"Expected a result for {state['queue'][0]!r}, got {qid!r}")

    quotes[qid] = {
        "company_name": quotes[qid]["company_name"],
        "total": call_result["total"],
        "binding": call_result["binding"],
        "red_flag": call_result.get("red_flag", quotes[qid].get("red_flag")),
        "source": "negotiated",
    }

    return {
        "quotes": quotes,
        "current_best_offer": compute_current_best(quotes),
        "queue": state["queue"][1:],
    }


def next_call_leverage(state: dict) -> dict | None:
    """Returns the current_best_offer to hand closer_agent.md for the NEXT queued
    call, or None if the next call IS the current best itself (no leverage to cite -
    see 'Calling the current best itself' in closer_orchestration.md)."""
    if not state["queue"]:
        return None
    next_qid = state["queue"][0]
    best = state["current_best_offer"]
    if best and best["quote_id"] == next_qid:
        return None
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("gathered_quotes", help="JSON file: {quote_id: {company_name, total, binding, red_flag?}}")
    args = parser.parse_args()

    gathered_quotes = json.loads(Path(args.gathered_quotes).read_text())
    state = start_round(gathered_quotes)

    print("Initial current_best_offer:", json.dumps(state["current_best_offer"], indent=2))
    print("\nCall order (as of round start - re-run/advance as real results come in):")
    for i, qid in enumerate(state["queue"], start=1):
        leverage = next_call_leverage({**state, "queue": state["queue"][i - 1:]})
        framing = f"cite ${leverage['total']:,.0f} from {leverage['company_name']}" if leverage else "no leverage - fee/terms pushback only"
        print(f"  {i}. {gathered_quotes[qid]['company_name']} ({framing})")


if __name__ == "__main__":
    main()
