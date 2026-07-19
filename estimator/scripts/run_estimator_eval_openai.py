"""
Same as run_estimator_eval.py but using the OpenAI API instead of Anthropic.
Runs the Estimator agent against a simulated customer persona for several turns,
extracts the final JSON spec, then grades it with the Evaluator agent.

Setup:
    pip install openai --break-system-packages
    export OPENAI_API_KEY=your_key_here

Usage:
    python run_estimator_eval_openai.py --persona A
    python run_estimator_eval_openai.py --persona B --turns 12
    python run_estimator_eval_openai.py --persona C --show-transcript
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TRANSCRIPTS_DIR = Path(__file__).parent.parent / "transcripts"

try:
    from openai import OpenAI
except ImportError:
    sys.exit("Missing dependency. Install with:\n    pip install openai --break-system-packages")

MODEL = "gpt-4o-mini"  # swap to whatever model your credits cover, e.g. "gpt-4o-mini" for cheaper/faster runs
DIR = Path(__file__).parent.parent

PERSONAS = {
    "A": {
        "name": "Cooperative",
        "system": """You are a customer planning a move. You are cooperative and give
clear, complete answers when asked. You are moving from a 2-bedroom apartment (2nd
floor, no elevator) to a 1-bedroom apartment (1st floor, elevator) about 45 miles away.
You have a sofa, queen bed, dresser, refrigerator, and about 20 boxes. No piano, no
storage needed. Move date is flexible, sometime in the next month. Answer only what's
asked - don't volunteer the whole inventory in your first message. If the estimator
agent asks you something that sounds like it's wrapping up (a summary/readback), just
confirm it's correct and say you're done.""",
        "ground_truth": {
            "origin": {"dwelling_type": "apartment", "floor": 2, "elevator": False},
            "destination": {"dwelling_type": "apartment", "floor": 1, "elevator": True},
            "distance_miles": 45,
            "large_items": ["sofa", "queen bed", "dresser", "refrigerator"],
            "estimated_boxes": 20,
            "storage_needed": False,
        },
    },
    "B": {
        "name": "Vague",
        "system": """You are a customer planning a move who gives short, vague answers
unless pushed. Same move details as: 2-bedroom apartment (2nd floor, no elevator) to
1-bedroom apartment (1st floor, elevator), 45 miles, sofa/queen bed/dresser/fridge/20
boxes, no piano, no storage. But when asked what you're moving, say "just normal
apartment stuff" first - only give specifics if the agent asks follow-up questions with
concrete examples. If asked about stairs, say "I think there's some stairs? Not sure"
until pressed for an exact number. Eventually give real answers if pushed twice.""",
        "ground_truth": {
            "origin": {"dwelling_type": "apartment", "floor": 2, "elevator": False},
            "destination": {"dwelling_type": "apartment", "floor": 1, "elevator": True},
            "distance_miles": 45,
            "large_items": ["sofa", "queen bed", "dresser", "refrigerator"],
            "estimated_boxes": 20,
            "storage_needed": False,
        },
    },
    "C": {
        "name": "Contradicts itself",
        "system": """You are a customer planning a move, same base details as: 2-bedroom
apartment (2nd floor, no elevator) to 1-bedroom apartment (1st floor, elevator), 45
miles, sofa/queen bed/dresser/fridge/20 boxes. Early in the conversation say there's no
piano. Partway through, when discussing large items again or near the end, contradict
yourself: "oh wait, I forgot, I do have a piano too." A good estimator should catch
this at readback.""",
        "ground_truth": {
            "origin": {"dwelling_type": "apartment", "floor": 2, "elevator": False},
            "destination": {"dwelling_type": "apartment", "floor": 1, "elevator": True},
            "distance_miles": 45,
            "large_items": ["sofa", "queen bed", "dresser", "refrigerator", "piano"],
            "estimated_boxes": 20,
            "storage_needed": False,
            "note": "customer initially denies piano, then reveals it later - final spec must include it",
        },
    },
}

EVALUATOR_SYSTEM = """You are grading a moving-company intake agent's performance. You
will be given GROUND TRUTH, TRANSCRIPT, and EXTRACTED JSON. Score 0-2 each on:
completeness, no_hallucination, critical_field_coverage, contradiction_handling,
conversation_efficiency. Output ONLY this JSON, no other text:
{"scores": {"completeness": 0, "no_hallucination": 0, "critical_field_coverage": 0,
"contradiction_handling": 0, "conversation_efficiency": 0}, "total": 0,
"failures": ["..."], "pass": false}
pass is true only if total >= 8 AND no_hallucination == 2."""


def load_estimator_prompt() -> str:
    base = (DIR / "estimator_agent.md").read_text()
    schema = (DIR / "schemas" / "job_spec.schema.json").read_text()
    harness_note = f"""

## Test harness note
You have no logging tool in this environment. Instead, once the user confirms the
summary, emit the final job spec as a single fenced code block like:

```json
{{ ... }}
```

matching this schema:

```json
{schema}
```

Output that code block and nothing else after it — no further pleasantries or
sign-off text in that message.
"""
    return base + harness_note


def chat(client, system: str, messages: list) -> str:
    """OpenAI chat completion helper - system goes in the messages list."""
    full_messages = [{"role": "system", "content": system}] + messages
    resp = client.chat.completions.create(model=MODEL, messages=full_messages)
    return resp.choices[0].message.content


def run_conversation(client, persona_key: str, max_turns: int, show_transcript: bool):
    persona = PERSONAS[persona_key]
    estimator_system = load_estimator_prompt()

    estimator_history = [{"role": "user", "content": "Begin the interview."}]
    customer_history = []
    transcript_lines = []
    estimator_text = ""

    for turn in range(max_turns):
        estimator_text = chat(client, estimator_system, estimator_history)
        estimator_history.append({"role": "assistant", "content": estimator_text})
        transcript_lines.append(f"ESTIMATOR: {estimator_text}")
        if show_transcript:
            print(f"\n[ESTIMATOR]: {estimator_text}")

        # Check if estimator produced final JSON (heuristic: contains a JSON block)
        if "```json" in estimator_text or re.search(r'\{\s*"origin"', estimator_text):
            break

        customer_history.append({"role": "user", "content": estimator_text})
        customer_text = chat(client, persona["system"], customer_history)
        customer_history.append({"role": "assistant", "content": customer_text})
        transcript_lines.append(f"CUSTOMER: {customer_text}")
        if show_transcript:
            print(f"[CUSTOMER]: {customer_text}")

        estimator_history.append({"role": "user", "content": customer_text})

    transcript = "\n".join(transcript_lines)
    return transcript, estimator_text


def extract_json_block(text: str) -> dict | None:
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def grade(client, persona_key: str, transcript: str, extracted: dict) -> dict:
    persona = PERSONAS[persona_key]
    user_content = (
        f"GROUND TRUTH:\n{json.dumps(persona['ground_truth'], indent=2)}\n\n"
        f"TRANSCRIPT:\n{transcript}\n\n"
        f"EXTRACTED JSON:\n{json.dumps(extracted, indent=2) if extracted else 'NONE - agent never produced valid JSON'}"
    )
    result_text = chat(client, EVALUATOR_SYSTEM, [{"role": "user", "content": user_content}])
    result = extract_json_block(result_text)
    return result or {"error": "evaluator did not return valid JSON", "raw": result_text}


def save_run(persona_key: str, transcript: str, extracted: dict, grading: dict) -> Path:
    """Write transcript.txt, extracted.json, and grading.json into a timestamped
    subfolder under transcripts/, e.g. transcripts/A_20260718_153000/"""
    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = TRANSCRIPTS_DIR / f"{persona_key}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "transcript.txt").write_text(transcript)
    (run_dir / "extracted.json").write_text(
        json.dumps(extracted, indent=2) if extracted else "null"
    )
    (run_dir / "grading.json").write_text(json.dumps(grading, indent=2))

    return run_dir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", choices=["A", "B", "C"], required=True)
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--show-transcript", action="store_true")
    args = parser.parse_args()

    client = OpenAI()  # reads OPENAI_API_KEY from env

    print(f"Running Persona {args.persona} ({PERSONAS[args.persona]['name']})...")
    transcript, final_estimator_text = run_conversation(
        client, args.persona, args.turns, args.show_transcript
    )
    extracted = extract_json_block(final_estimator_text)

    print("\n--- GRADING ---")
    result = grade(client, args.persona, transcript, extracted)
    print(json.dumps(result, indent=2))

    run_dir = save_run(args.persona, transcript, extracted, result)
    print(f"\nSaved transcript, extracted JSON, and grading to: {run_dir}")


if __name__ == "__main__":
    main()