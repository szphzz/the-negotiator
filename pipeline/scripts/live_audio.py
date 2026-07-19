"""
Turn-by-turn live audio: generate speech for one line of dialogue via OpenAI TTS and
play it out loud immediately (macOS `afplay`), so a pipeline run can be heard as it
happens instead of only played back afterward. Also saves each turn's audio file
alongside wherever the run's transcript is being saved, so a full live run leaves
behind the same kind of playable archive generate_audio.py produces after the fact.

No dialogue content lives here - only the generate-and-play plumbing.
"""

import subprocess
import sys
from pathlib import Path

from generate_audio import DEFAULT_VOICE_MAP, FALLBACK_VOICE, DEFAULT_MODEL


def speak_turn(client, role: str, text: str, turn_number: int, save_dir: Path,
               voice_map: dict | None = None, model: str = DEFAULT_MODEL, subtitles=None) -> Path:
    """Generate audio for one turn, save it, show it on the subtitle window (if given)
    right as playback starts, play it out loud (blocks until playback finishes, so
    turns are heard in order at natural pace), then return the file path."""
    voice_map = voice_map or DEFAULT_VOICE_MAP
    voice = voice_map.get(role, FALLBACK_VOICE)

    response = client.audio.speech.create(model=model, voice=voice, input=text)
    save_dir.mkdir(parents=True, exist_ok=True)
    audio_path = save_dir / f"turn_{turn_number:02d}_{role}.mp3"
    audio_path.write_bytes(response.read())

    if subtitles:
        subtitles.show(role, text)

    if sys.platform == "darwin":
        subprocess.run(["afplay", str(audio_path)])
    else:
        print(f"  (audio saved to {audio_path} - live playback only implemented for macOS/afplay)")

    return audio_path


def make_turn_counter():
    """Returns a callable that hands back 1, 2, 3, ... on each call - one shared
    counter per conversation so audio filenames stay in correct playback order."""
    count = 0
    def next_number():
        nonlocal count
        count += 1
        return count
    return next_number
