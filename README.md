# The Negotiator

Submission for **Challenge 01: The Negotiator**, Hack-Nation's 6th Global AI Hackathon
(in collaboration with MIT Club of Northern California, MIT Club of Germany, and
powered by ElevenLabs).

> Voice agents that call, compare, and haggle — pick your market, never overpay again.

## The problem

Phone-priced markets — moving, medical billing, car buying, contractor bids, freight —
share three failures: opaque pricing (a documented 5.6x quote spread for one identical
45-mile move, $1,158–$6,506), unreliable sight-unseen estimates (40% more likely to end
in a final bill above quote, per FMCSA), and no practical way for a consumer to shop a
market of thousands of phone-and-paper small operators. Fixing this means calling 5–8
businesses, describing the same job identically every time, extracting itemized quotes,
and negotiating — which almost nobody has time to do by hand.

This project picks **residential moving** as the vertical, scoped to **California**.

## How it works

Three agents share one job spec and hand off a growing body of evidence:

| Module | Directory | Role |
|---|---|---|
| 01 The Estimator | [estimator/](estimator/) | Builds the structured job spec via voice interview and/or document intake (photos, quotes, inventory lists) — same schema either way, confirmed by the user before any calls go out. |
| 02 The Caller | [caller/](caller/) | Phones each candidate mover, describes the job identically every time, and extracts a structured, itemized quote or a documented callback/decline. |
| 03 The Closer | [closer/](closer/) | Calls companies back, uses the strongest competing quote as leverage, applies red-flag rules (30%+ below market), and reports a ranked, evidence-backed comparison. |
| Market discovery (supporting) | [dataset/](dataset/) | Finds candidate moving companies and pulls Google review data to build the call list the Caller works from — research only, never calls or negotiates. |

[schemas/](schemas/) holds the two cross-module contracts every stage reads and writes:
`job_spec.schema.json` (the Estimator's output, reused verbatim by every downstream
call) and `comparison_report.schema.json` (the Closer's final ranked output).

```
Estimator (job spec) → Caller (itemized quotes) → Closer (negotiated, ranked) → report
```

## Required behaviors

- Intake offers **both** a voice interview (ElevenLabs Agents) and document intake,
  converging on the identical job spec.
- The Caller demo shows live calls against **at least three distinct negotiation
  styles** — tough / lowballer-with-hidden-fees / hard-sell upseller, plus a
  stonewaller — each ending in exactly one structured outcome: itemized quote, callback
  commitment, or documented decline, never a vague number.
- At least one negotiation shows price or terms **measurably move during the call**
  because of leverage the Closer gathered — not a scripted outcome.
- AI disclosure is mandatory: agents state plainly that they're an AI calling on a
  customer's behalf if asked, without breaking the call.
- Agents never invent inventory, a fake competing bid, or misrepresent the job — a hard
  constraint across the Caller and Closer, not a style preference.
- The loop closes end to end: intake → calls → negotiation → ranked recommendation with
  transcript evidence.

## Current state

The **voice** side (ElevenLabs Agents) isn't wired up yet — `estimator_agent.md`,
`caller_agent.md`, and `closer_agent.md` in each module are the system prompts intended
for it. Today every module is built and evaluated as **text-only conversations against
simulated counterparties via the OpenAI API**, driven by each module's
`scripts/run_*_eval_openai.py` and persona set (`estimator/customer_simulator.md`,
`caller/counterparty_personas.md`, `closer/negotiation_scenarios.md`).

[pipeline/run_pipeline.py](pipeline/run_pipeline.py) chains all three stages into one
automated run end to end — intake, four simulated company calls, a full negotiation
round with ratcheting leverage and red-flag exclusion, and a final ranked
`comparison_report.json` — reusing each module's existing eval-harness building blocks
rather than forking them. Runs are saved under `pipeline/runs/<timestamp>/`.

The dataset/company-research stage (`dataset/webscraper_agent.md`) is real Google
Places research, not simulated — `dataset/ca_candidates_raw.json` holds partial raw
candidate output (975 of a 2,000 target) from an in-progress California-wide pull.

## Setup

```bash
pip install openai jsonschema --break-system-packages
export OPENAI_API_KEY=your_key_here
```

## Running the modules

**Full pipeline** (Estimator → Caller → Closer → report, one command):

```bash
python pipeline/run_pipeline.py
python pipeline/run_pipeline.py --customer-persona vague --show-transcript
python pipeline/run_pipeline.py --companies carolina,budget --no-redflag
python pipeline/run_pipeline.py --turns-estimator 20 --turns-caller 10 --turns-closer 10
```

Output for each run lands in `pipeline/runs/<timestamp>/`: the confirmed job spec, the
quotes fed into the report, and the final `comparison_report.json`.

**Individual modules**, each run against a simulated persona and graded by that
module's evaluator:

```bash
# Estimator — simulated customer personas A / B / C
python estimator/scripts/run_estimator_eval_openai.py --persona A
python estimator/scripts/run_estimator_eval_openai.py --persona C --show-transcript

# Caller — counterparty personas: tough / lowballer / upseller / stonewaller
python caller/scripts/run_caller_eval_openai.py --persona tough
python caller/scripts/run_caller_eval_openai.py --persona lowballer --show-transcript

# Closer — single callback against one scenario
python closer/scripts/run_closer_eval_openai.py --scenario tough
python closer/scripts/run_closer_eval_openai.py --scenario redflag

# Closer — full negotiation round across all four callbacks at once, with
# current_best_offer ratcheting and red-flag exclusion carried across calls
python closer/scripts/run_negotiation_round.py --show-transcript
```

Each script saves its transcript and extracted JSON outcome under that module's
`transcripts/` directory.

## Repository layout

```
estimator/   Job-spec intake (voice interview + document intake), evaluator, transcripts
caller/      Company outreach agent, counterparty personas, evaluator, transcripts
closer/      Callback/negotiation agent, orchestration logic, config, transcripts
dataset/     Google Places-based moving company research (real data, not simulated)
pipeline/    End-to-end driver chaining Estimator → Caller → Closer → report
schemas/     Canonical job_spec and comparison_report JSON Schemas, with examples
```
