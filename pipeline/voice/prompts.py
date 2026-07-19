"""
Voice-specific prompt loaders for the Estimator <-> Customer Realtime demo.

Reuses the exact same behavior prompt and personas as the text pipeline
(estimator/estimator_agent.md, pipeline/run_pipeline.py's CUSTOMER_PERSONAS) -
the only difference from the text harness (run_estimator_eval_openai.py's
load_estimator_prompt) is the closing instruction: call a Realtime tool instead
of emitting a fenced JSON code block, since a voice session can't "speak" JSON
usefully but can call a real function.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

import run_pipeline  # noqa: E402 - reuse CUSTOMER_PERSONAS and the initial spec

ESTIMATOR_HARNESS_NOTE = """

## Voice harness note
You have a `submit_job_spec` tool available in this environment. Once the
customer confirms your final readback, call `submit_job_spec` with the
complete job spec as its arguments - real data gathered in this conversation,
not the schema itself. The reference schema below (JSON Schema, draft-07)
describes the shape and field names to use. Do NOT copy its structure
verbatim - it contains JSON-Schema keywords like "type", "properties",
"required", and "enum" that must never appear as values in your tool call.
Your arguments should contain only the actual field names (e.g. "address",
"zip", "floor") mapped directly to real values (e.g. "123 Main St", "94110", 2).

```json
{schema}
```

Do not speak the JSON aloud - call the tool. Set `user_confirmed` to true only
because the customer just confirmed the readback. After calling the tool, give
one brief spoken sign-off line and stop.
"""


def load_voice_estimator_prompt() -> str:
    base = (ROOT / "estimator" / "estimator_agent.md").read_text()
    schema = (ROOT / "schemas" / "job_spec.schema.json").read_text()
    return base + ESTIMATOR_HARNESS_NOTE.format(schema=schema)


def load_voice_customer_prompt(persona_key: str) -> str:
    return run_pipeline.CUSTOMER_PERSONAS[persona_key]


def load_initial_spec() -> dict:
    return run_pipeline.load_initial_spec()


CUSTOMER_PERSONA_KEYS = list(run_pipeline.CUSTOMER_PERSONAS.keys())
