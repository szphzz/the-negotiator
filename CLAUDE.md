# CLAUDE.md

## What this repo is

This is a submission for **The Negotiator**, Challenge 01 of Hack-Nation's 6th Global
AI Hackathon (in collaboration with MIT Club of Northern California and MIT Club of
Germany, powered by ElevenLabs). Full brief: *"Voice agents that call, compare, and
haggle — pick your market, never overpay again."*

### The problem (from the challenge brief)

Phone-priced markets — moving, medical billing, car buying, contractor bids, freight —
share the same three failures: opaque pricing (a 5.6x quote spread was documented for
one identical 45-mile move, $1,158–$6,506), unreliable sight-unseen estimates (40% more
likely to end in a final bill above quote, per FMCSA), and no practical way for a
consumer to shop a market of thousands of phone-and-paper small operators. The fix
requires someone (or something) with the patience to call 5–8 businesses, describe the
same job identically every time, extract itemized quotes, and negotiate — which almost
nobody has time to do by hand.

### The chosen vertical: residential moving

This repo picks **moving** as the vertical, scoped to **California** (see
`dataset/webscraper_agent.md`'s Mode 2 regional pool). The three required modules map
directly onto this repo's top-level directories:

| Brief module | This repo | Role |
|---|---|---|
| 01 The Estimator | [estimator/](estimator/) | Builds the structured job spec via voice interview and/or document intake (photos, quotes, inventory lists) — same schema either way, confirmed by the user before any calls go out. |
| 02 The Caller | [caller/](caller/) | Phones each candidate mover, describes the job identically every time, and extracts a structured, itemized quote or a documented callback/decline. |
| 03 The Closer | [closer/](closer/) | Calls companies back, uses the strongest competing quote as leverage, applies red-flag rules (30%+ below market), and reports a ranked, evidence-backed comparison. |
| (supporting) Market discovery | [dataset/](dataset/) | Finds candidate moving companies and pulls Google review data to build the call list the Caller works from — research only, never calls or negotiates. |

`schemas/` holds the canonical cross-module contracts: `job_spec.schema.json` (the
Estimator's output, reused verbatim by every downstream call) and
`comparison_report.schema.json` (the Closer's final ranked output).

### Required by the brief, worth keeping in view

- Intake must offer **both** a voice interview (ElevenLabs Agents) and at least one
  document-intake path, converging on the identical job spec.
- The Caller's demo must show live calls against **at least three distinct negotiation
  styles** (tough / lowballer-with-hidden-fees / hard-sell upseller, plus optionally a
  stonewaller), each ending in exactly one structured outcome: itemized quote, callback
  commitment, or documented decline — never a vague number.
- At least one negotiation must show price or terms **measurably move during the call**
  because of leverage the Closer gathered — not a scripted outcome.
- AI disclosure is mandatory: agents must say plainly that they're an AI calling on a
  customer's behalf if asked, without breaking the call.
- Never invent inventory, a fake competing bid, or misrepresent the job — this is a
  hard constraint across Caller and Closer, not a style preference.
- The loop must close end to end: intake → calls → negotiation → ranked recommendation
  with transcript evidence.

### Current state / gaps

- The **voice** side (ElevenLabs Agents) is not yet wired up — `estimator_agent.md`,
  `caller_agent.md`, and `closer_agent.md` are the system prompts intended for it.
  Today, each module is iterated on and evaluated as **text-only conversations against
  simulated counterparties via the OpenAI API** (see each module's
  `scripts/run_*_eval_openai.py` and its persona set: `estimator/customer_simulator.md`,
  `caller/counterparty_personas.md`, `closer/negotiation_scenarios.md`) — this is
  explicitly the fast-iteration step before moving to real voice.
- The dataset/company-research stage (`dataset/webscraper_agent.md`) is real Google
  Places research, not simulated — `dataset/ca_candidates_raw.json` holds partial
  raw candidate output from an in-progress California-wide pull.
- `closer/scripts/plan_negotiation_round.py` implements the call-order and ratcheting
  `current_best_offer` logic from `closer/closer_orchestration.md`;
  `closer/scripts/run_negotiation_round.py` drives a full simulated round with it.
- `closer/scripts/build_comparison_report.py` builds the final ranked report
  (`schemas/comparison_report.schema.json`) from a set of gathered quotes — pure data
  logic, no LLM call needed once quotes are structured.
- The three per-module eval scripts and the closer's round driver each run in
  isolation today; there is no single script yet that chains a job spec through
  Estimator → Caller → Closer → final report as one connected pipeline.
