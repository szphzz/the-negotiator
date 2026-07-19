# Closer Orchestration — call order and the ratcheting "current best offer"

This sits ABOVE `closer_agent.md`. The per-call agent is stateless across calls — it
only ever sees whatever `current_best_offer` it's handed for that one call. This doc
defines how that value is computed and updated between calls, and in what order
companies get called back. Whoever drives the negotiation round (a script, or a human
running calls one at a time) is responsible for this — not the LLM agent itself.

## The `current_best_offer` object
```json
{
  "quote_id": "carolina",
  "company_name": "Carolina Movers Co.",
  "total": 2200,
  "binding": true,
  "source": "negotiated"
}
```
`source` is `"original"` (straight from The Caller, untouched) or `"negotiated"` (came
out of an earlier Closer callback this round).

## Selection rule — what counts as "current best"
Among all quotes gathered so far (original Caller quotes, updated by any Closer
callback results so far this round):
1. **Exclude red-flagged quotes entirely.** A quote flagged per
   `market_benchmarks.json`'s red-flag rule is not eligible to be cited as leverage —
   it's a quote that's probably missing fees, and using it to pressure a legitimate
   company means potentially citing a number that isn't real. If a flagged quote gets
   unflagged after a Closer callback confirms it's clean (see `red_flag.resolution` in
   `closer_agent.md`), it becomes eligible from that point on.
2. **Prefer binding over non-binding.** Among eligible quotes, take the cheapest
   BINDING one. Only fall back to the cheapest non-binding quote if no binding quote
   exists yet — and when you do, `current_best_offer` should carry that non-binding
   status through so the agent can frame it honestly ("I have a quote around $X,
   though it's not binding yet").
3. Ties: prefer the quote from the company most recently confirmed (freshest number).

## Call order
1. Compute the initial `current_best_offer` from the Caller's original quotes only
   (rule above).
2. Call every OTHER company (i.e. every quote_id that isn't the current best),
   ordered from highest original total to lowest. This hits the biggest gaps first,
   where there's the most room to negotiate, and builds pressure before the harder
   cases.
3. After each call: if the result beats `current_best_offer` (same rule as above —
   binding-preferred, and only if not itself red-flagged), update
   `current_best_offer` to that new number before placing the next call. A company
   that was called early and lost can end up displaced by a later company's number
   for calls further down the queue.
4. Once every other company has been called once, place one FINAL call to whichever
   company is `current_best_offer` at that point — but frame it differently (see
   "Calling the current best itself" below), since there's no cheaper number left to
   cite against it.
5. Stop. This is one full round. Re-running additional rounds (calling companies back
   a second time) is an optional extension, not required for the MVP — most gains
   happen in the first pass, and repeated callbacks risk annoying counterparties.

## Calling the current best itself
When `current_best_offer` IS the company being called (step 4), there is no
competitor number to cite — citing the company's own quote back at them is
meaningless. `closer_agent.md` handles this case explicitly: skip the "can you beat
this" framing entirely and negotiate purely on fees, deposit terms, and extras
(packing materials, a held rate for longer) instead. The expected outcomes here are
`partial_concession` or `held_firm`, never `price_matched` (there's nothing to match
against).

## Reference implementation
`closer/scripts/plan_negotiation_round.py` implements the selection rule and call
order above, and exposes `advance_round()` to recompute `current_best_offer` after
each real call outcome — use it to drive calls one at a time rather than
re-implementing this logic ad hoc.
