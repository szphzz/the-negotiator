"""
Validate a job spec (produced by either the voice interview or document extraction
path) against schemas/job_spec.schema.json before it's allowed to be used in any
outbound call.

Usage:
    python validate_job_spec.py path/to/spec.json

Install dependency first:
    pip install jsonschema --break-system-packages
"""

import json
import sys
import hashlib
from pathlib import Path

try:
    from jsonschema import Draft7Validator
except ImportError:
    sys.exit(
        "Missing dependency. Install with:\n"
        "    pip install jsonschema --break-system-packages"
    )

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "job_spec.schema.json"

# Fields that MUST be present and non-null before a spec can be used on a real call,
# even though the JSON Schema itself only enforces top-level required keys.
CRITICAL_FIELDS = [
    ("origin", "zip"),
    ("origin", "dwelling_type"),
    ("origin", "floor"),
    ("destination", "zip"),
    ("destination", "dwelling_type"),
    ("destination", "floor"),
    ("move_date",),
    ("access_conditions", "origin_stairs_flights"),
    ("access_conditions", "destination_stairs_flights"),
    ("access_conditions", "origin_long_carry"),
    ("access_conditions", "destination_long_carry"),
]


def load_schema() -> dict:
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def get_nested(d: dict, path: tuple):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def compute_spec_hash(spec: dict) -> str:
    """Deterministic hash of the spec content (excluding the hash field itself and
    user_confirmed), used to prove every call used the exact same confirmed spec."""
    spec_copy = {k: v for k, v in spec.items() if k not in ("spec_version_hash",)}
    canonical = json.dumps(spec_copy, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def validate(spec_path: str) -> bool:
    with open(spec_path) as f:
        spec = json.load(f)

    schema = load_schema()
    validator = Draft7Validator(schema)
    schema_errors = sorted(validator.iter_errors(spec), key=lambda e: e.path)

    ok = True

    if schema_errors:
        ok = False
        print(f"SCHEMA ERRORS ({len(schema_errors)}):")
        for err in schema_errors:
            field_path = ".".join(str(p) for p in err.path) or "(root)"
            print(f"  - {field_path}: {err.message}")

    missing_critical = []
    for path in CRITICAL_FIELDS:
        if get_nested(spec, path) is None:
            missing_critical.append(".".join(path))

    if missing_critical:
        ok = False
        print(f"\nMISSING CRITICAL FIELDS ({len(missing_critical)}):")
        print("  These are schema-optional but required before calling — they're the")
        print("  fields that most commonly cause day-of upcharges if left blank:")
        for field in missing_critical:
            print(f"  - {field}")

    if not spec.get("user_confirmed", False):
        ok = False
        print("\nNOT USER-CONFIRMED:")
        print("  user_confirmed is false/missing. Do not use this spec on a real call")
        print("  until the user has reviewed and explicitly confirmed it.")

    if ok:
        computed_hash = compute_spec_hash(spec)
        stored_hash = spec.get("spec_version_hash")
        print("VALID — spec is complete and confirmed.")
        print(f"  computed hash: {computed_hash}")
        if stored_hash and stored_hash != computed_hash:
            print(f"  WARNING: stored hash {stored_hash} does not match computed hash.")
            print("  The spec may have been edited after confirmation without")
            print("  re-confirming. Every call must log the CURRENT hash.")
        elif not stored_hash:
            print("  NOTE: no spec_version_hash stored yet. Set it to the computed")
            print("  hash above at confirmation time, then log that hash on every call.")

    return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python validate_job_spec.py path/to/spec.json")

    valid = validate(sys.argv[1])
    sys.exit(0 if valid else 1)
