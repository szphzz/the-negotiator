"""
Realtime API tool (function-calling) schema for the Estimator's voice session.

Derived from schemas/job_spec.schema.json rather than hand-written, so the voice
path and the text pipeline (pipeline/run_pipeline.py) stay in sync with one
source of truth for what a confirmed job spec looks like.
"""

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
JOB_SPEC_SCHEMA_PATH = ROOT / "schemas" / "job_spec.schema.json"


def _strip_schema_meta(schema: dict) -> dict:
    """Drop JSON-Schema document-level keys that don't belong inside a Realtime
    tool's `parameters` object (draft-07's `$schema`/`title` describe the schema
    document itself, not the parameter shape a tool call should match)."""
    schema = dict(schema)
    schema.pop("$schema", None)
    schema.pop("title", None)
    return schema


def build_submit_job_spec_tool() -> dict:
    """Realtime tool objects are flat (type/name/description/parameters as
    siblings), unlike Chat Completions' nested `function: {...}` shape."""
    schema = json.loads(JOB_SPEC_SCHEMA_PATH.read_text())
    return {
        "type": "function",
        "name": "submit_job_spec",
        "description": (
            "Submit the final confirmed job spec once the customer has "
            "confirmed the readback summary. Call this exactly once, at the "
            "end of the interview, with real data gathered on the call."
        ),
        "parameters": _strip_schema_meta(schema),
    }
