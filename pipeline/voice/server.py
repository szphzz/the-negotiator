"""
Backend for the Estimator <-> Customer voice demo (OpenAI Realtime API, WebRTC).

Mints ephemeral Realtime client secrets server-side (the real OPENAI_API_KEY
never reaches the browser) and resolves each side's instructions/tools, reusing
the text pipeline's exact prompts, personas, and spec validation rather than
forking them. The actual WebRTC SDP exchange happens directly between the
browser and OpenAI (POST /v1/realtime/calls) - this server is only the token
mint + prompt resolver + static file host.

Setup:
    pip install flask --break-system-packages
    export OPENAI_API_KEY=your_key_here

Run:
    python pipeline/voice/server.py
    # then open http://localhost:5050
    # (port 5050, not 5000 - macOS's AirPlay Receiver squats on 5000 by default)
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

HERE = Path(__file__).parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "pipeline"))

import prompts  # noqa: E402
import tools  # noqa: E402
import run_pipeline  # noqa: E402 - validate_confirmed_spec + validator.compute_spec_hash

REALTIME_CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"

# One voice per side so the live transcript/audio in the demo is easy to tell apart.
ROLE_CONFIG = {
    "estimator": {
        "model": "gpt-realtime",
        "voice": "marin",
        "instructions": prompts.load_voice_estimator_prompt,
        "tools": [tools.build_submit_job_spec_tool()],
    },
    "customer": {
        "model": "gpt-realtime",
        "voice": "cedar",
        "instructions": None,  # resolved per-request from the chosen persona
        "tools": [],
    },
}

app = Flask(__name__, static_folder=None)


def mint_client_secret(model: str, voice: str, instructions: str, tool_defs: list) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in the environment.")

    body = {
        "expires_after": {"anchor": "created_at", "seconds": 300},
        "session": {
            "type": "realtime",
            "model": model,
            "audio": {"output": {"voice": voice}},
            "instructions": instructions,
            "output_modalities": ["audio"],
            "tools": tool_defs,
            "tool_choice": "auto" if tool_defs else "none",
        },
    }
    req = urllib.request.Request(
        REALTIME_CLIENT_SECRETS_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Realtime client_secrets mint failed ({e.code}): {e.read().decode()}")


@app.route("/")
def index():
    return send_from_directory(HERE / "static", "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(HERE / "static", filename)


@app.route("/api/persona-keys")
def persona_keys():
    return jsonify({"personas": prompts.CUSTOMER_PERSONA_KEYS})


@app.route("/api/initial-spec")
def initial_spec():
    return jsonify(prompts.load_initial_spec())


@app.route("/api/session", methods=["POST"])
def create_session():
    payload = request.get_json(force=True)
    role = payload.get("role")
    if role not in ROLE_CONFIG:
        return jsonify({"error": f"unknown role '{role}', expected one of {list(ROLE_CONFIG)}"}), 400

    config = ROLE_CONFIG[role]
    if role == "customer":
        persona_key = payload.get("persona")
        if persona_key not in prompts.CUSTOMER_PERSONA_KEYS:
            return jsonify({"error": f"unknown persona '{persona_key}'"}), 400
        instructions = prompts.load_voice_customer_prompt(persona_key)
    else:
        instructions = config["instructions"]()

    try:
        minted = mint_client_secret(config["model"], config["voice"], instructions, config["tools"])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "client_secret": minted.get("value"),
        "expires_at": minted.get("expires_at"),
        "model": config["model"],
        "voice": config["voice"],
        "tools": config["tools"],
    })


@app.route("/api/validate-spec", methods=["POST"])
def validate_spec():
    spec = request.get_json(force=True)
    problems = run_pipeline.validate_confirmed_spec(spec)
    result = {"valid": not problems, "problems": problems}
    if not problems:
        result["spec_version_hash"] = run_pipeline.validator.compute_spec_hash(spec)
    return jsonify(result)


if __name__ == "__main__":
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("Set OPENAI_API_KEY before starting the server.")
    app.run(port=5050, debug=True)
